import { identityForBundleId, type VariableIdentity } from "./identity";
import { isobaricRange, temperaturePaletteDomain } from "./levels";
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
// The low end (<= 2 mm/h) climbs from nearly transparent to opaque, which is
// what makes drizzle legible over the dark theme's basemap. One ramp serves
// both themes, so on the light theme's white ground those same faint blues
// read weaker than they do on a slate — a known trade, taken so a value is
// the same color in both themes rather than two palettes drifting apart.
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

// Radar composite reflectivity, in the dBZ classes a Chinese (and US) radar
// mosaic is read in: cyan and blue drizzle, green light rain, yellow through
// red for the convective core, magenta and violet for hail-sized echoes.
// The classes are interpolated rather than stepped because the shader
// reconstructs the code field bicubically and blends two frames before the
// lookup — hard class edges would shimmer as codes cross them. Alpha rises
// from nothing at 0 dBZ, which is both "no echo" and "outside radar
// coverage": a mosaic covers only the land its network watches, and the
// codebook has no separate value for the difference.
const REFLECTIVITY_STOPS: Stop[] = [
  [0, 0, 236, 236, 0],
  [5, 0, 236, 236, 70],
  [10, 1, 160, 246, 130],
  [15, 0, 80, 236, 185],
  [20, 0, 216, 96, 220],
  [25, 0, 200, 0, 235],
  [30, 0, 144, 0, 245],
  [35, 255, 255, 0, 255],
  [40, 231, 192, 0, 255],
  [45, 255, 144, 0, 255],
  [50, 255, 0, 0, 255],
  [55, 214, 0, 0, 255],
  [60, 192, 0, 0, 255],
  [65, 255, 0, 240, 255],
  [70, 150, 0, 180, 255],
  [80, 240, 233, 255, 255],
];

// The pressure family shares one ramp, given in fractions of the level's own
// codebook range rather than absolute values: a fill under contour lines is
// read as "low here, high there", and every level would otherwise need its
// own table of metres. Low-saturation on purpose — the lines are the
// subject, and the fill is composited beneath them at partial alpha, so a
// vivid ramp would compete with them for the eye. Cool for troughs and lows,
// warm for ridges and highs, the way a filled height chart is conventionally
// coloured.
const PRESSURE_UNIT_STOPS: Stop[] = [
  [0.0, 52, 66, 120, 255],
  [0.2, 62, 104, 148, 255],
  [0.4, 82, 142, 150, 255],
  [0.55, 128, 166, 136, 255],
  [0.7, 186, 172, 116, 255],
  [0.85, 196, 132, 100, 255],
  [1.0, 168, 82, 92, 255],
];

/** The pressure ramp mapped onto one variable's own codebook range. */
function pressureStops(quantization: LinearQuantization): Stop[] {
  const low = quantization.offset;
  const high = quantization.offset + quantization.scale * quantization.maximumCode;
  return PRESSURE_UNIT_STOPS.map(
    ([unit, r, g, b, a]) => [low + unit * (high - low), r, g, b, a] as Stop,
  );
}

/** A ramp given over one range, laid over another: the stop values move
 * linearly, the colours stay. */
function remapStops(stops: Stop[], from: readonly [number, number], to: readonly [number, number]): Stop[] {
  const scale = (to[1] - to[0]) / (from[1] - from[0]);
  return stops.map(([value, r, g, b, a]) => [to[0] + (value - from[0]) * scale, r, g, b, a] as Stop);
}

// Relative humidity, the way a moisture chart is read: dry air in the browns
// of bare ground, the middle in paper tones, saturated air in greens deepening
// to blue past 90 %, where cloud and rain live. Opaque like temperature — the
// field covers everything, and there is no "nothing here" to let the map
// through.
const HUMIDITY_STOPS: Stop[] = [
  [0, 128, 84, 40, 235],
  [20, 176, 132, 84, 235],
  [40, 216, 196, 150, 235],
  [55, 224, 224, 196, 235],
  [70, 170, 212, 170, 240],
  [80, 104, 184, 140, 245],
  [90, 56, 144, 160, 250],
  [100, 36, 84, 160, 255],
];

// Specific humidity in fractions of the level's codebook ceiling: dry air is
// transparent (the map reads through as it does under precipitation), and
// the moist end runs pale green through teal to a deep blue.
const SPECIFIC_HUMIDITY_UNIT_STOPS: Stop[] = [
  [0.0, 200, 220, 200, 0],
  [0.08, 190, 222, 170, 120],
  [0.2, 140, 204, 140, 180],
  [0.35, 90, 184, 150, 215],
  [0.5, 60, 150, 170, 235],
  [0.7, 50, 104, 176, 245],
  [1.0, 56, 48, 140, 255],
];

// Water vapour flux magnitude, g·cm⁻¹·hPa⁻¹·s⁻¹. Nothing below 2 — dry or
// calm air is the map — then greens into teal, blue and violet for the
// conveyor belts a rainstorm feeds on (20–40 is strong transport).
export const VAPOUR_FLUX_STOPS: Stop[] = [
  [0, 160, 210, 170, 0],
  [2, 160, 210, 170, 60],
  [5, 130, 200, 140, 140],
  [10, 90, 186, 150, 190],
  [15, 70, 164, 176, 215],
  [20, 60, 132, 196, 232],
  [30, 72, 96, 200, 245],
  [40, 110, 70, 190, 252],
  [50, 140, 50, 170, 255],
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

/** The speed field's own transparency, by speed: the ramp above was drawn
 * for particles over a bare map and is opaque, but as a filled field it has
 * to let the ground read through — a calm sea is mostly map, and even a gale
 * keeps a little of it, the way the precipitation palette does. */
const WIND_FIELD_ALPHA: [number, number][] = [
  [0, 0.4],
  [10, 0.55],
  [20, 0.7],
  [40, 0.8],
];

function windFieldAlpha(speed: number): number {
  const first = WIND_FIELD_ALPHA[0]!;
  const last = WIND_FIELD_ALPHA[WIND_FIELD_ALPHA.length - 1]!;
  if (speed <= first[0]) return first[1];
  if (speed >= last[0]) return last[1];
  for (let index = 1; index < WIND_FIELD_ALPHA.length; index += 1) {
    const [upperSpeed, upperAlpha] = WIND_FIELD_ALPHA[index]!;
    if (speed > upperSpeed) continue;
    const [lowerSpeed, lowerAlpha] = WIND_FIELD_ALPHA[index - 1]!;
    return lowerAlpha + ((speed - lowerSpeed) / (upperSpeed - lowerSpeed)) * (upperAlpha - lowerAlpha);
  }
  return last[1];
}

/** The same ramp as a filled field: indexed by speed like the particle
 * palette, with the field's transparency folded in. `maxSpeed` is the
 * speed the last entry stands for; the ramp and its transparency stretch
 * with it, so an isobaric wind with a higher ceiling keeps the same colours
 * at the same fraction of its own scale. */
export function buildWindFieldPalette(maxSpeed = WIND_SPEED_MAX): Uint8Array {
  const palette = buildWindSpeedPalette(maxSpeed);
  const stretch = maxSpeed / WIND_SPEED_MAX;
  for (let index = 0; index < 256; index += 1) {
    const speed = (index / 255) * maxSpeed;
    palette[index * 4 + 3] = Math.round(palette[index * 4 + 3]! * windFieldAlpha(speed / stretch));
  }
  return palette;
}

/** Build the 256x1 RGBA speed palette for the wind particle layer: index i
 * maps speed (i / 255) * maxSpeed m/s to a color; faster wind above the
 * ramp ceiling keeps the last color. */
export function buildWindSpeedPalette(maxSpeed = WIND_SPEED_MAX): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  const stops = maxSpeed === WIND_SPEED_MAX ? WIND_SPEED_STOPS : remapStops(WIND_SPEED_STOPS, [0, WIND_SPEED_MAX], [0, maxSpeed]);
  for (let index = 0; index < 256; index += 1) {
    const speed = (index / 255) * maxSpeed;
    palette.set(interpolate(stops, speed), index * 4);
  }
  return palette;
}

/** The vapour flux magnitude palette, indexed like the wind field's: entry i
 * is the flux (i / 255) * maxMagnitude. */
export function buildVapourFluxPalette(maxMagnitude: number): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  for (let index = 0; index < 256; index += 1) {
    palette.set(interpolate(VAPOUR_FLUX_STOPS, (index / 255) * maxMagnitude), index * 4);
  }
  return palette;
}

/** A CSS gradient reading a palette top-down — the legend bar of a field
 * whose key is not hand-written in the stylesheet. `from`/`to` pick the index
 * range the legend spans (a codebook's valid codes, or the whole magnitude
 * scale). */
export function legendGradient(palette: Uint8Array, from = 0, to = 255, samples = 12): string {
  const stops: string[] = [];
  for (let sample = 0; sample < samples; sample += 1) {
    const index = Math.round(to - (sample / (samples - 1)) * (to - from));
    const at = index * 4;
    stops.push(`rgba(${palette[at]}, ${palette[at + 1]}, ${palette[at + 2]}, ${(palette[at + 3]! / 255).toFixed(3)})`);
  }
  return `linear-gradient(to bottom, ${stops.join(", ")})`;
}

/** The codebook's own coverage, in the variable's unit — the range an
 * unrecognized field's ramp is spread over. */
function codebookRange(quantization: LinearQuantization): readonly [number, number] {
  return [quantization.offset, quantization.offset + quantization.scale * quantization.maximumCode];
}

/** Color stops for one variable's physical values, chosen by what the field
 * *is* (identity.ts) rather than by what it is called: dswrf and cref carry
 * their own ramps, the pressure family reads its codebook, and the isobaric
 * fills take theirs from the family and the level. A field this build does
 * not recognize gets the temperature ramp spread over its own codebook, so
 * it is at least legible. */
function stopsFor(variable: BundleVariable, identity: VariableIdentity | null): Stop[] {
  const linear = variable.quantization.type === "linear" ? variable.quantization : null;
  if (identity === null) {
    return linear ? remapStops(TEMPERATURE_STOPS, [-60, 50], codebookRange(linear)) : TEMPERATURE_STOPS;
  }
  const { family, level } = identity;
  if (family === "dswrf") return SOLAR_STOPS;
  if (family === "cref") return REFLECTIVITY_STOPS;
  if (family === "hgt" && linear) return pressureStops(linear);
  if (family === "tmp") return remapStops(TEMPERATURE_STOPS, [-60, 50], temperaturePaletteDomain(level));
  if (family === "rh") return HUMIDITY_STOPS;
  if (family === "spfh") {
    const range = isobaricRange("spfh", level);
    if (range) return remapStops(SPECIFIC_HUMIDITY_UNIT_STOPS, [0, 1], [0, range[1]]);
  }
  return TEMPERATURE_STOPS;
}

/** One code in a variable's own codebook, or null for the reserved codes
 * (no data, and anything past the codebook's maximum). */
export function decodeValue(variable: BundleVariable, code: number): number | null {
  return variable.quantization.type === "linear"
    ? decodeLinear(variable.quantization, code)
    : decodeLog(variable.quantization, code);
}

/** Build the 256x1 RGBA palette texture for one variable's code space.
 *
 * `identity` is what the field is, from its parameter block; omit it and the
 * id string is read as the naming convention instead, which is all a poster
 * (metadata without an open session) has. An explicit `null` means the field
 * is not one this build recognizes. */
export function buildPalette(
  variable: BundleVariable,
  identity: VariableIdentity | null = identityForBundleId(variable.id),
): Uint8Array {
  const palette = new Uint8Array(256 * 4);
  const stops = stopsFor(variable, identity);
  for (let code = 0; code < 256; code += 1) {
    let color: [number, number, number, number] = [0, 0, 0, 0];
    const value = decodeValue(variable, code);
    if (value !== null) {
      // A logarithmic codebook's zero code is dry, and dry is not painted.
      if (variable.quantization.type === "linear") color = interpolate(stops, value);
      else if (value > 0) color = interpolate(PRECIPITATION_STOPS, value);
    }
    palette.set(color, code * 4);
  }
  return palette;
}
