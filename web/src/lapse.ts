/**
 * The 2 m temperature carried from the model's ground to a finer terrain's.
 *
 * A quarter-degree model stands a summit on a broad hill hundreds or
 * thousands of metres too low, and its 2 m temperature belongs to that
 * hill. Below the model ground (a valley) the standard lapse rate carries it
 * down as well as anything the model's column could. Above it a fixed rate
 * fails at night: it carries the model ground's surface inversion up to a
 * summit that stands in the free atmosphere. So above the model ground the
 * temperature is the free atmosphere's at the site — the run's isobaric
 * temperatures interpolated in their heights — plus the model's own
 * departure from that free atmosphere at its ground, faded with the height
 * climbed:
 *
 *   T = free(site) + exp(−dz / H) · (tmp2m − free(model ground))
 *
 * The map's fragment shader (`SITE_TEMPERATURE_GLSL`) and the meteogram's
 * terrain row (`siteTemperature`) are this one formula, so the two agree at
 * the pin up to how each samples the grid.
 */

/** Standard atmosphere lapse rate, kelvin per metre: below the model ground,
 * past either end of the column, and wherever the run carries no column. */
export const LAPSE_RATE = 0.0065;

/** How fast the model's boundary-layer departure fades with the height
 * climbed above its ground, metres. Fitted on four seasonal weeks of
 * mountain stations against GFS and ECMWF; each model's own best is within
 * 0.01 K of this one. */
export const SURFACE_ANOMALY_DECAY_M = 800;

/** The surfaces the column is read on, bottom first: a summit the
 * correction serves stands below 500 hPa, and the levels above it add
 * nothing. A run uses those it publishes both the temperature and the
 * height of. */
export const COLUMN_LEVELS_HPA = [1000, 925, 850, 700, 500] as const;

/** The most surfaces a column holds — the shader's fixed loop bound. */
export const COLUMN_CAPACITY = COLUMN_LEVELS_HPA.length;

/** One surface of the column: its geopotential height (m) and its
 * temperature (°C). */
export interface ColumnLevel {
  height: number;
  temperature: number;
}

/** A column as both readers want it: its surfaces sorted by height, the
 * ones without both numbers left out. Fewer than two is no column. */
export function sortedColumn(levels: readonly ColumnLevel[]): ColumnLevel[] | null {
  const kept = levels.filter((level) => Number.isFinite(level.height) && Number.isFinite(level.temperature));
  if (kept.length < 2) return null;
  return [...kept].sort((a, b) => a.height - b.height);
}

/** The free atmosphere's temperature at `height`: linear between the
 * column's surfaces, at the standard lapse rate past either end. `column`
 * is sorted (`sortedColumn`). */
export function freeAtmosphere(column: readonly ColumnLevel[], height: number): number {
  const first = column[0]!;
  const last = column[column.length - 1]!;
  if (height < first.height) return first.temperature - LAPSE_RATE * (height - first.height);
  if (height > last.height) return last.temperature - LAPSE_RATE * (height - last.height);
  for (let index = 1; index < column.length; index += 1) {
    const upper = column[index]!;
    if (height > upper.height) continue;
    const lower = column[index - 1]!;
    const span = upper.height - lower.height;
    if (span <= 0) return upper.temperature;
    return lower.temperature + ((upper.temperature - lower.temperature) * (height - lower.height)) / span;
  }
  return last.temperature;
}

/** The 2 m temperature at `siteHeight`, from the model's at `modelHeight`
 * (both metres). Below the model ground, and above it without a column
 * (`null`), at the standard lapse rate — `fallback` says the column was
 * wanted and missing. */
export function siteTemperature(
  t2m: number,
  modelHeight: number,
  siteHeight: number,
  column: readonly ColumnLevel[] | null,
): { value: number; fallback: boolean } {
  const dz = siteHeight - modelHeight;
  if (dz <= 0) return { value: t2m - LAPSE_RATE * dz, fallback: false };
  if (column === null || column.length < 2) return { value: t2m - LAPSE_RATE * dz, fallback: true };
  const fade = Math.exp(-dz / SURFACE_ANOMALY_DECAY_M);
  return {
    value: freeAtmosphere(column, siteHeight) + fade * (t2m - freeAtmosphere(column, modelHeight)),
    fallback: false,
  };
}

/** The same formula in GLSL, as the kelvin a fragment adds to its 2 m
 * temperature: `heights` / `temperatures` are the column sorted by height,
 * `count` surfaces of them (0 or 1 where there is no column). */
export const SITE_TEMPERATURE_GLSL = `
const int COLUMN_CAPACITY = ${COLUMN_CAPACITY};
float freeAtmosphere(float heights[COLUMN_CAPACITY], float temperatures[COLUMN_CAPACITY], int count, float z) {
  if (z < heights[0]) return temperatures[0] - ${LAPSE_RATE.toFixed(4)} * (z - heights[0]);
  float result = temperatures[count - 1] - ${LAPSE_RATE.toFixed(4)} * (z - heights[count - 1]);
  bool found = false;
  for (int index = 1; index < COLUMN_CAPACITY; index += 1) {
    if (index >= count || found || z > heights[index]) continue;
    float span = heights[index] - heights[index - 1];
    result = span <= 0.0
      ? temperatures[index]
      : temperatures[index - 1] + (temperatures[index] - temperatures[index - 1]) * (z - heights[index - 1]) / span;
    found = true;
  }
  return result;
}
float siteTemperatureDelta(float t2m, float modelHeight, float siteHeight,
    float heights[COLUMN_CAPACITY], float temperatures[COLUMN_CAPACITY], int count) {
  float dz = siteHeight - modelHeight;
  if (dz <= 0.0 || count < 2) return -${LAPSE_RATE.toFixed(4)} * dz;
  float fade = exp(-dz / ${SURFACE_ANOMALY_DECAY_M.toFixed(1)});
  float site = freeAtmosphere(heights, temperatures, count, siteHeight)
    + fade * (t2m - freeAtmosphere(heights, temperatures, count, modelHeight));
  return site - t2m;
}
`;
