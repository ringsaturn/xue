/** The model forecast of a static airport page, read: each model's live run,
 * and its series at the grid cell nearest the airport, through the
 * comparison page's reader (`compare/series.ts`: one decode worker per
 * bundle, the cell's chunks in one round trip, the worker closed again).
 *
 * `live.ts` imports this module only once the reports are on the page, so
 * the decoder never holds back the METAR. A model whose run or bundle cannot
 * be read is left out of the chart; the block says so only when none is. */

import { fetchModelRun, publishedOf, readPointSeries, seriesValues, type PointSeries } from "../compare/series";
import type { ForecastBundleId, ForecastModelId, LoadedManifest } from "../manifest";
import type { Metar } from "../stations/schema";
import { convertTemperature, convertWind, variableDisplayUnit, variableDisplayValue } from "../units";
import { airportModelLabel, airportModels, renderModelChart, type ChartPoint, type ChartRow } from "./modelchart";
import type { LiveConfig } from "./pages";

/** The rows, in order: the bundle each reads and the quantity off a report. */
const ROWS: readonly { bundle: ForecastBundleId; observed: ((metar: Metar) => number | null) | null; baseline: number | null }[] = [
  { bundle: "tmp2m", observed: (metar) => (metar.t === null ? null : convertTemperature(metar.t)), baseline: null },
  { bundle: "prate", observed: null, baseline: 0 },
  { bundle: "wind10m", observed: (metar) => (metar.ws === null ? null : convertWind(metar.ws)), baseline: 0 },
];

interface ModelRead {
  model: ForecastModelId;
  run: LoadedManifest;
  series: Map<ForecastBundleId, PointSeries>;
}

async function readModel(base: string, model: ForecastModelId, config: LiveConfig): Promise<ModelRead | null> {
  try {
    const run = await fetchModelRun(base, model);
    const series = new Map<ForecastBundleId, PointSeries>();
    for (const bundle of publishedOf(run, ROWS.map((row) => row.bundle))) {
      try {
        const read = await readPointSeries(run, bundle, config.lon, config.lat);
        if (read) series.set(bundle, read);
      } catch (error) {
        console.warn(`airport: ${model} ${bundle} not read:`, error instanceof Error ? error.message : error);
      }
    }
    return series.size > 0 ? { model, run, series } : null;
  } catch (error) {
    console.warn(`airport: ${model} run not read:`, error instanceof Error ? error.message : error);
    return null;
  }
}

function points(series: PointSeries): ChartPoint[] {
  const values = seriesValues(series);
  const variable = series.variables[0]!;
  return series.validTimesMs.flatMap((t, index) => {
    const value = values[index];
    return value === null || value === undefined ? [] : [{ t, v: variableDisplayValue(variable, value) }];
  });
}

function runHour(run: LoadedManifest): string {
  const time = new Date(run.manifest.runTime);
  return `${String(time.getUTCDate()).padStart(2, "0")}/${String(time.getUTCHours()).padStart(2, "0")}Z`;
}

/** Fill `root` with the chart, or a note when no model could be read. */
export async function renderModels(root: HTMLElement, config: LiveConfig, base: string, metars: readonly Metar[]): Promise<void> {
  const w = config.words;
  const status = root.querySelector<HTMLElement>("[data-status]");
  if (status) status.textContent = w.modelsLoading;
  const reads = (await Promise.all(airportModels(config.lon, config.lat).map((model) => readModel(base, model, config)))).filter(
    (read): read is ModelRead => read !== null,
  );
  const titles: Record<string, string> = { tmp2m: w.temperature, prate: w.precipitation, wind10m: w.wind };
  const rows: ChartRow[] = ROWS.flatMap((spec) => {
    const first = reads.map((read) => read.series.get(spec.bundle)).find((series) => series !== undefined);
    if (!first) return [];
    return [
      {
        title: titles[spec.bundle]!,
        unit: variableDisplayUnit(first.variables[0]!),
        traces: reads.map((read) => {
          const series = read.series.get(spec.bundle);
          return { label: airportModelLabel(read.model), points: series ? points(series) : [] };
        }),
        observed: spec.observed
          ? metars.flatMap((metar) => {
              const v = spec.observed!(metar);
              return v === null ? [] : [{ t: Date.parse(metar.time), v }];
            })
          : [],
        baseline: spec.baseline,
      },
    ];
  });
  const html = renderModelChart({
    nowMs: Date.now(),
    tz: config.tz,
    lang: config.lang,
    rows,
    legend: reads.map((read) => `${airportModelLabel(read.model)} ${runHour(read.run)}`),
    observedLabel: w.observed,
    nowLabel: w.now,
  });
  if (!html) {
    if (status) status.textContent = w.modelsUnavailable;
    return;
  }
  const content = document.createElement("div");
  content.className = "models";
  content.innerHTML = html;
  status?.remove();
  root.append(content);
}
