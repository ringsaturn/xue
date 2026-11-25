/**
 * The section's fills: the site's ramps laid over a whole column instead of
 * one surface, cut into steps. A level's own window would paint most of the
 * column one colour, and a stepped edge is what makes a front or an inversion
 * a line.
 */

import { identityForBundleId } from "../identity";
import { thetaEPaletteDomain } from "../levels";
import type { BundleVariable, LinearQuantization } from "../manifest";
import { buildPalette, windFieldStops } from "../palettes";
import type { SectionFill } from "./types";

type Rgb = readonly [number, number, number];
type Stop = readonly [value: number, color: Rgb];

/** What a legend's numbers are in, before the reader's units: K, °C, % and
 * m/s. */
export type SectionUnit = "kelvin" | "temperature" | "percent" | "wind";

/** A stepped ramp as the chart paints it: band `k` covers `min + k * step` to
 * the next, and values past either end take the end's colour. */
export interface SectionBands {
  min: number;
  step: number;
  /** Three bytes a band. */
  rgb: Uint8Array;
}

export interface SectionLegend {
  unit: SectionUnit;
  min: number;
  max: number;
  /** The bands, ascending. */
  stops: { from: number; to: number; color: string }[];
  /** The values to label, inside the range. */
  ticks: number[];
}

interface Ramp {
  unit: SectionUnit;
  min: number;
  max: number;
  step: number;
  tick: number;
  stops: () => Stop[];
}

/** The colours of a bundle's own palette at values its quantization below
 * lands on exactly. The id is not a published one, so that a ramp a host page
 * set for the map's field is not stretched over a range it was not made for. */
function sitePalette(bundle: string, offset: number, scale: number): (value: number) => Rgb {
  const quantization: LinearQuantization = {
    type: "linear",
    offset,
    scale,
    minimumCode: 0,
    maximumCode: 254,
    nodataCode: 255,
  };
  const variable = { numericId: 1, id: "section", label: "", unit: "", quantization } as unknown as BundleVariable;
  const palette = buildPalette(variable, identityForBundleId(bundle));
  return (value) => {
    const at = Math.round((value - offset) / scale) * 4;
    return [palette[at]!, palette[at + 1]!, palette[at + 2]!];
  };
}

function thetaEStops(): Stop[] {
  const [low, high] = thetaEPaletteDomain(850);
  const site = sitePalette("thetae850", low, 0.5);
  const stops = [0, 0.15, 0.3, 0.45, 0.6, 0.72, 0.85, 1].map((share): Stop => {
    const value = low + share * (high - low);
    return [value, site(value)];
  });
  // The stratosphere runs far past the warmest air mass: one darkening tail
  // keeps it from looking like a tropical one.
  return [...stops, [400, [78, 14, 62]]];
}

/** The surface ramp from -30 °C up, so that 0 °C is its colour on the map;
 * the cold end is stretched down to the tropopause. */
function temperatureStops(): Stop[] {
  const site = sitePalette("tmp2m", -100, 1);
  return [
    [-80, site(-60)],
    [-60, site(-50)],
    [-45, site(-40)],
    ...[-30, -20, -10, 0, 10, 20, 30].map((value): Stop => [value, site(value)]),
  ];
}

/** The map's wash, opaque here: its driest brown and its paper are lightened
 * for it. */
function humidityStops(): Stop[] {
  const site = sitePalette("rh850", 0, 1);
  return [
    [0, [164, 118, 70]],
    [20, site(20)],
    [40, site(40)],
    [55, [240, 238, 222]],
    ...[70, 80, 90, 100].map((value): Stop => [value, site(value)]),
  ];
}

/** A ramp's colour at a value, its ends' past them. */
function colorAt(stops: readonly Stop[], value: number): Rgb {
  let upper = 1;
  while (upper < stops.length - 1 && stops[upper]![0] < value) upper += 1;
  const [from, low] = stops[upper - 1]!;
  const [to, high] = stops[upper]!;
  const weight = Math.min(1, Math.max(0, (value - from) / (to - from)));
  return [0, 1, 2].map((channel) => Math.round(low[channel]! + weight * (high[channel]! - low[channel]!))) as unknown as Rgb;
}

/** Paper up to a fresh breeze, then the surface ramp at twice its speeds: the
 * jet is the subject. */
function windStops(): Stop[] {
  const site = windFieldStops();
  const at = (speed: number): Rgb => colorAt(
    site.map(([value, r, g, b]): Stop => [value, [r, g, b]]),
    speed,
  );
  return [
    [0, [236, 234, 226]],
    [10, [206, 216, 222]],
    [20, [120, 180, 190]],
    [30, at(10)],
    [40, at(15)],
    [50, at(20)],
    [60, at(25)],
    [70, at(30)],
    [80, at(40)],
  ];
}

/** Two-sided: into the page in blues, out of it in reds. */
const CROSS_STOPS: Stop[] = [
  [-90, [8, 28, 70]],
  [-60, [20, 60, 130]],
  [-40, [45, 105, 175]],
  [-20, [120, 170, 210]],
  [-5, [225, 228, 226]],
  [5, [230, 226, 222]],
  [20, [225, 165, 130]],
  [40, [200, 95, 65]],
  [60, [130, 35, 40]],
  [90, [64, 10, 28]],
];

const RAMPS: Record<SectionFill, Ramp> = {
  thetae: { unit: "kelvin", min: 252, max: 400, step: 4, tick: 20, stops: thetaEStops },
  tmp: { unit: "temperature", min: -80, max: 32, step: 4, tick: 20, stops: temperatureStops },
  rh: { unit: "percent", min: 0, max: 100, step: 10, tick: 20, stops: humidityStops },
  wind: { unit: "wind", min: 0, max: 80, step: 5, tick: 10, stops: windStops },
  cross: { unit: "wind", min: -90, max: 90, step: 5, tick: 30, stops: () => CROSS_STOPS },
};

const built = new Map<SectionFill, SectionBands>();

/** The fill's bands, each the ramp's colour at its middle. */
export function sectionBands(fill: SectionFill): SectionBands {
  const known = built.get(fill);
  if (known) return known;
  const { min, max, step, stops: makeStops } = RAMPS[fill];
  const stops = makeStops();
  const count = Math.round((max - min) / step);
  const rgb = new Uint8Array(count * 3);
  for (let band = 0; band < count; band += 1) {
    rgb.set(colorAt(stops, min + (band + 0.5) * step), band * 3);
  }
  const bands: SectionBands = { min, step, rgb };
  built.set(fill, bands);
  return bands;
}

/** The fill's legend, in the units of `unit`. */
export function sectionLegend(fill: SectionFill): SectionLegend {
  const { unit, min, max, tick } = RAMPS[fill];
  const { step, rgb } = sectionBands(fill);
  const stops = Array.from({ length: rgb.length / 3 }, (_, band) => ({
    from: min + band * step,
    to: min + (band + 1) * step,
    color: `rgb(${rgb[band * 3]}, ${rgb[band * 3 + 1]}, ${rgb[band * 3 + 2]})`,
  }));
  const ticks: number[] = [];
  for (let value = Math.ceil(min / tick) * tick; value <= max; value += tick) ticks.push(value);
  return { unit, min, max, stops, ticks };
}
