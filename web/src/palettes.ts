import type { BundleVariable, LinearQuantization, LogQuantization } from "./manifest";

/**
 * Color ramps in physical units. The shader samples them per pixel, so the
 * decoded planes stay single-channel and coloring is a pure GPU concern.
 */
type Stop = [value: number, r: number, g: number, b: number, a: number];

const TEMPERATURE_STOPS: Stop[] = [
  [-60, 39, 25, 89, 255],
  [-50, 49, 54, 149, 255],
  [-40, 55, 103, 190, 255],
  [-30, 65, 155, 201, 255],
  [-20, 111, 201, 183, 255],
  [-10, 181, 226, 174, 255],
  [0, 238, 239, 179, 255],
  [10, 254, 217, 118, 255],
  [20, 253, 153, 66, 255],
  [30, 230, 85, 48, 255],
  [40, 179, 32, 55, 255],
  [50, 112, 20, 65, 255],
];

// The top of the ramp deepens through violet instead of washing out to
// near-white, and extends to the codebook's 128 mm/h ceiling so extreme
// cells keep their own gradation rather than sharing one color above 50.
// The low end (<= 2 mm/h) runs light and opaque: drizzle must stay readable
// over the dark basemap instead of sinking into it.
const PRECIPITATION_STOPS: Stop[] = [
  [0, 8, 19, 28, 0],
  [0.05, 64, 131, 183, 70],
  [0.1, 82, 157, 205, 120],
  [0.5, 96, 186, 219, 175],
  [1, 90, 210, 200, 205],
  [2, 111, 219, 158, 225],
  [5, 213, 225, 101, 235],
  [10, 255, 190, 75, 245],
  [20, 245, 112, 72, 250],
  [30, 205, 61, 98, 255],
  [40, 155, 46, 133, 255],
  [50, 118, 37, 138, 255],
  [80, 84, 28, 125, 255],
  [128, 48, 14, 84, 255],
];

// Solar radiation ramp: night stays transparent so
// the terminator reads as the basemap fading through, then an inferno-like
// progression — deep violet dawn light through orange to near-white noon
// glare at the 1270 W/m² codebook ceiling. Values are W/m².
const SOLAR_STOPS: Stop[] = [
  [0, 12, 8, 34, 0],
  [30, 36, 15, 82, 80],
  [100, 84, 21, 110, 140],
  [200, 130, 37, 108, 180],
  [350, 180, 54, 92, 210],
  [500, 222, 81, 65, 230],
  [650, 246, 118, 34, 242],
  [800, 252, 163, 27, 250],
  [950, 250, 208, 58, 255],
  [1100, 252, 240, 130, 255],
  [1270, 255, 253, 210, 255],
];

// Wind speed ramp for the GPU particle layer: the
// familiar blue -> teal -> green -> yellow -> orange -> red -> violet
// progression (earth.nullschool / Windy convention). Values are m/s.
export const WIND_SPEED_MAX = 40;
const WIND_SPEED_STOPS: Stop[] = [
  [0, 110, 124, 195, 255],
  [5, 82, 157, 191, 255],
  [10, 92, 190, 140, 255],
  [15, 191, 205, 92, 255],
  [20, 235, 167, 74, 255],
  [25, 238, 112, 66, 255],
  [30, 212, 60, 87, 255],
  [35, 170, 51, 133, 255],
  [40, 129, 55, 168, 255],
];

function interpolate(stops: Stop[], value: number): [number, number, number, number] {
  const first = stops[0]!;
  const last = stops[stops.length - 1]!;
  if (value <= first[0]) return [first[1], first[2], first[3], first[4]];
  if (value >= last[0]) return [last[1], last[2], last[3], last[4]];
  for (let index = 1; index < stops.length; index += 1) {
    const upper = stops[index]!;
    if (value <= upper[0]) {
      const lower = stops[index - 1]!;
      const t = (value - lower[0]) / (upper[0] - lower[0]);
      return [
        Math.round(lower[1] + (upper[1] - lower[1]) * t),
        Math.round(lower[2] + (upper[2] - lower[2]) * t),
        Math.round(lower[3] + (upper[3] - lower[3]) * t),
        Math.round(lower[4] + (upper[4] - lower[4]) * t),
      ];
    }
  }
  return [last[1], last[2], last[3], last[4]];
}

export function decodeLinear(quantization: LinearQuantization, code: number): number | null {
  if (code === quantization.nodataCode || code > quantization.maximumCode) return null;
  return quantization.offset + code * quantization.scale;
}

export function decodeLog(quantization: LogQuantization, code: number): number | null {
  if (code === quantization.nodataCode) return null;
  if (code === quantization.zeroCode) return 0;
  if (code > quantization.overflowCode) return null;
  // The overflow code extends the logarithmic grid one step past the maximum.
  const lo = Math.log1p(quantization.trace / quantization.scale);
  const hi = Math.log1p(quantization.maximum / quantization.scale);
  const unit = (code - quantization.minimumCode) / (quantization.maximumCode - quantization.minimumCode);
  return quantization.scale * Math.expm1(lo + unit * (hi - lo));
}

/** Build the 256x1 RGBA speed palette for the wind particle layer: index i
 * maps speed (i / 255) * WIND_SPEED_MAX m/s to a color; faster wind above the
 * ramp ceiling keeps the last color. */
export function buildWindSpeedPalette(): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  for (let index = 0; index < 256; index += 1) {
    const speed = (index / 255) * WIND_SPEED_MAX;
    palette.set(interpolate(WIND_SPEED_STOPS, speed), index * 4);
  }
  return palette;
}

/** Color stops for one variable's physical values. Linear fields default to
 * the temperature ramp; dswrf carries its own solar ramp. */
function stopsFor(variable: BundleVariable): Stop[] {
  if (variable.id === "dswrf") return SOLAR_STOPS;
  return TEMPERATURE_STOPS;
}

/** Build the 256x1 RGBA palette texture for one variable's code space. */
export function buildPalette(variable: BundleVariable): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  for (let code = 0; code < 256; code += 1) {
    let color: [number, number, number, number] = [0, 0, 0, 0];
    if (variable.quantization.type === "linear") {
      const value = decodeLinear(variable.quantization, code);
      if (value !== null) color = interpolate(stopsFor(variable), value);
    } else {
      const value = decodeLog(variable.quantization, code);
      if (value !== null && value > 0) color = interpolate(PRECIPITATION_STOPS, value);
    }
    palette.set(color, code * 4);
  }
  return palette;
}
