/**
 * The section in the reader's units: what its axes and its legend are
 * labelled with, and what the worker draws in them, which knows no units.
 * Every step is round in the unit it is read in, not converted from one that
 * is round in another. Pressure is in hPa whatever the pressure unit: the
 * axis and the levels are named so.
 */

import { formatDistance } from "../measure";
import { convertHeight, convertTemperature, convertWind, formatTemperature, UNIT_LABELS, units, type WindUnit } from "../units";
import { pressureY } from "./chart";
import type { SectionLegend, SectionUnit } from "./ramps";
import type { SectionDrawn, SectionDrawUnits } from "./types";

/** What a step of 1 m/s becomes in each unit, as steps go: a round one of
 * about the same size. */
const WIND_STEP: Record<WindUnit, number> = { ms: 1, kmh: 4, kt: 2, mph: 2 };

/** Isotachs, m/s: every ten from the strength of a gale in the making. */
const ISOTACH_INTERVAL = 10;
const ISOTACH_FIRST = 20;
/** Metres per second from which an isotach is a jet's, and drawn heavier. */
const JET_SPEED = 50;

/** The least step of the height axis, metres: a kilometre, or 5,000 ft. */
export function heightBase(): number {
  return units.height === "m" ? 1000 : 5000 / convertHeight(1, "ft");
}

export function drawUnits(): SectionDrawUnits {
  const step = WIND_STEP[units.wind];
  return {
    wind: {
      scale: convertWind(1),
      interval: ISOTACH_INTERVAL * step,
      first: ISOTACH_FIRST * step,
      heavy: convertWind(JET_SPEED),
    },
    freezing: formatTemperature(0, 0),
    heightStep: heightBase(),
  };
}

/** A round height of the right axis: kilometres, or thousands of feet. */
export function axisHeight(metres: number): string {
  return units.height === "m" ? `${Math.round(metres / 1000)} km` : `${Math.round(convertHeight(metres) / 1000)}k ft`;
}

/** The round heights there is room to label: every least step, or every second,
 * third or fourth where labels would come closer than `spacing` pixels. */
export function axisHeights(heights: SectionDrawn["heights"], top: number, height: number, spacing = 20): SectionDrawn["heights"] {
  const base = heightBase();
  // Not those still drawn in the unit of before a switch.
  const inside = heights.filter(
    ({ metres, pressure }) => pressure >= top && Math.abs(metres / base - Math.round(metres / base)) < 1e-6,
  );
  const every = (step: number) => inside.filter(({ metres }) => Math.round(metres / base) % step === 0);
  const ys = (kept: SectionDrawn["heights"]) => kept.map(({ pressure }) => pressureY(pressure, top, height));
  for (const step of [1, 2, 3]) {
    const kept = every(step);
    if (ys(kept).every((y, index, all) => index === 0 || Math.abs(y - all[index - 1]!) >= spacing)) return kept;
  }
  return every(4);
}

/** A distance along the line. The start is a zero of the unit the others are
 * in, not of the short one. */
export function axisDistance(metres: number): string {
  return metres === 0 ? `0 ${UNIT_LABELS.distance[units.distance]}` : formatDistance(metres);
}

/** The legend's labels, where each is and what it says. */
export function legendTicks({ unit, min, max, ticks }: Pick<SectionLegend, "unit" | "min" | "max" | "ticks">): { value: number; label: string }[] {
  const shown = (value: number): number => (unit === "temperature" ? convertTemperature(value) : unit === "wind" ? convertWind(value) : value);
  const spacing = ticks.length > 1 ? ticks[1]! - ticks[0]! : max - min;
  const step =
    unit === "wind" ? spacing * WIND_STEP[units.wind] : unit === "temperature" && units.temperature === "F" ? spacing * 2 : spacing;
  const zero = shown(0);
  const scale = shown(1) - zero;
  const found: { value: number; label: string }[] = [];
  for (let at = Math.ceil(shown(min) / step) * step; at <= shown(max) + 1e-9; at += step) {
    // Not a negative zero, which would be written as one.
    found.push({ value: (at - zero) / scale, label: String(Math.round(at) + 0) });
  }
  return found;
}

export function legendUnit(unit: SectionUnit): string {
  switch (unit) {
    case "kelvin":
      return "K";
    case "percent":
      return "%";
    case "temperature":
      return UNIT_LABELS.temperature[units.temperature];
    case "wind":
      return UNIT_LABELS.wind[units.wind];
  }
}
