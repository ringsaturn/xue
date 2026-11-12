/**
 * Which statistic of an ensemble a field is, as the shell keys it.
 *
 * A file says it in the `ensemble` block beside the parameter
 * (`BundleEnsemble`, docs/format.md "Ensemble"); the shell folds the block
 * into one short key so an identity can carry it and compare it by
 * equality: `mean` and `spread` (derived forecasts 0 and 4), `d<code>` for
 * any other derived forecast, `q<percentile>`, and `p<type>:<lower>:<upper>`
 * for a probability, each limit the threshold in the quantity's GRIB2 unit
 * (blank where the type uses none). A single forecast has no key.
 *
 * A mean or a percentile is still the quantity — it reads in its unit, on
 * its palette — so a chart keyed by the quantity draws it; a spread or a
 * probability is a different field (another unit, another range) and never
 * draws as the quantity (identity.ts).
 */

import { t } from "./i18n";
import type { ChartFamily } from "./identity";
import type { BundleEnsemble } from "./manifest";

/** Code table 4.7: the unweighted mean of all members, and their spread
 * (the standard deviation about the mean). */
const DERIVED_MEAN = 0;
const DERIVED_SPREAD = 4;

/** A limit's value, `scaledValue × 10^-scaleFactor`, rounded past float
 * noise so `2 / 1000` reads as 10 rather than 9.999999999. */
function limitValue(scale: number | null, value: number | null): number | null {
  if (scale === null || value === null) return null;
  return Math.round(value * 10 ** -scale * 1e9) / 1e9;
}

/** The key of one `ensemble` block. */
export function statisticKey(ensemble: BundleEnsemble): string {
  if ("derivedForecast" in ensemble) {
    if (ensemble.derivedForecast === DERIVED_MEAN) return "mean";
    if (ensemble.derivedForecast === DERIVED_SPREAD) return "spread";
    return `d${ensemble.derivedForecast}`;
  }
  if ("percentileValue" in ensemble) return `q${ensemble.percentileValue}`;
  const lower = limitValue(ensemble.scaleFactorOfLowerLimit, ensemble.scaledValueOfLowerLimit);
  const upper = limitValue(ensemble.scaleFactorOfUpperLimit, ensemble.scaledValueOfUpperLimit);
  return `p${ensemble.probabilityType}:${lower ?? ""}:${upper ?? ""}`;
}

/** The percentile a key names, or null for any other statistic. */
export function statisticPercentile(key: string | undefined): number | null {
  const match = key === undefined ? null : /^q(\d+)$/.exec(key);
  return match ? Number(match[1]) : null;
}

/** A probability key's type (code table 4.9) and limits, or null. */
export function statisticProbability(
  key: string | undefined,
): { type: number; lower: number | null; upper: number | null } | null {
  const match = key === undefined ? null : /^p(\d+):([^:]*):([^:]*)$/.exec(key);
  if (!match) return null;
  const limit = (text: string) => (text === "" ? null : Number(text));
  return { type: Number(match[1]), lower: limit(match[2]!), upper: limit(match[3]!) };
}

/** Whether a statistic is still the quantity it is of — a mean, a
 * percentile, any derived forecast but the spread — and so charts as it. */
export function chartsAsQuantity(key: string | undefined): boolean {
  if (key === undefined) return true;
  return key !== "spread" && statisticProbability(key) === null;
}

/** The statistic a field's centre is: the median of a percentile set, the
 * mean beside a spread. */
export const MEDIAN_KEY = "q50";
export const MEAN_KEY = "mean";
export const SPREAD_KEY = "spread";

/** The chart family of each family's spread, where one is charted: the
 * spread reads in the quantity's unit but over a range of its own, from
 * zero, so it is a family of its own rather than the quantity's. */
export const SPREAD_FAMILIES: Partial<Record<ChartFamily, ChartFamily>> = { hgt: "hgtsd" };

/** The family a spread family is the spread of, or null. */
export function spreadBaseFamily(family: ChartFamily): ChartFamily | null {
  for (const [base, spread] of Object.entries(SPREAD_FAMILIES)) if (spread === family) return base as ChartFamily;
  return null;
}

/** The short localized note a reading carries for its statistic ("median",
 * "75th pct", "spread", "P>10 mm"), or null for a single forecast. `unit` is
 * the unit a probability's threshold reads in. */
export function statisticNote(key: string | undefined, unit: string): string | null {
  if (key === undefined) return null;
  if (key === MEAN_KEY) return t("statMean");
  if (key === SPREAD_KEY) return t("statSpread");
  const percentile = statisticPercentile(key);
  if (percentile !== null) return percentile === 50 ? t("statMedian") : t("statPercentile", { n: percentile });
  const probability = statisticProbability(key);
  if (probability) {
    const { type, lower, upper } = probability;
    const value = (limit: number | null) => `${limit ?? ""}${unit ? ` ${unit}` : ""}`;
    // Code table 4.9: below / above / equal to the lower limit, above /
    // below the upper one, between the two.
    if (type === 0) return `P<${value(lower)}`;
    if (type === 3) return `P>${value(lower)}`;
    if (type === 5) return `P=${value(lower)}`;
    if (type === 1) return `P>${value(upper)}`;
    if (type === 4) return `P<${value(upper)}`;
    if (type === 2) return `P ${lower ?? ""}–${value(upper)}`;
  }
  return t("statDerived", { code: key.slice(1) });
}
