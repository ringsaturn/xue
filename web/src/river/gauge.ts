/**
 * The gauge section of the probe panel: a river case's gauge, opened by a
 * click on its mark, as a hydrograph under the meteogram rows — what the
 * gauge measured, what the model carried there, the peaks its operator
 * reported — laid on the same two columns as the rows, code and readouts
 * on the left, the chart over the capsule's track on the right.
 *
 * The section owns its DOM and the gauge's observed line (one range read
 * of `gauges.jsonl`, made once per gauge). It does not own the modelled
 * series: main.ts hands that in at render time, from the track's store on
 * a network case or from the case's own discharge field on a grid one, so
 * a track switch or a frame landing is just a repaint.
 */

import { element } from "../dom";
import { htmlLang, t } from "../i18n";
import { drawHydrograph, valueNear, type HydrographData, type HydrographPeak, type TimedSeries } from "./hydrograph";
import { fetchGaugeSeries, type GaugeSeries, type LoadedRiverIndex, type RiverGauge } from "./index";

/** The chart's height, CSS pixels: four or five decades readable. */
const CHART_HEIGHT = 120;
const FONT = '8px "IBM Plex Mono", ui-monospace, monospace';
/** How far from the playhead a reading still speaks for it. */
const READOUT_TOLERANCE_MS = 45 * 60 * 1000;

/** `m3/s` as it reads. */
export function unitLabel(unit: string): string {
  return unit === "m3/s" ? "m³/s" : unit;
}

/** A flow to three significant figures in the UI locale's digits. */
export function formatFlow(value: number): string {
  if (!Number.isFinite(value)) return "--";
  const digits = value >= 100 ? 0 : value >= 10 ? 1 : 2;
  return new Intl.NumberFormat(htmlLang, { maximumFractionDigits: digits, maximumSignificantDigits: 3 }).format(value);
}

export interface GaugeRenderInput {
  runTimeMs: number;
  leadSeconds: readonly number[];
  selected: number;
  /** The playhead's valid time. */
  time: number;
  modelled: TimedSeries | null;
  /** A time, formatted the way the panel formats valid times. */
  formatTime: (time: number) => string;
  ink: { ink: string; muted: string };
}

export interface GaugeSection {
  root: HTMLElement;
  canvas: HTMLCanvasElement;
  gauge(): RiverGauge | null;
  setGauge(loaded: LoadedRiverIndex | null, gauge: RiverGauge | null): void;
  render(input: GaugeRenderInput): void;
}

type LineState = GaugeSeries | "pending" | "failed" | null;

export function createGaugeSection(onLoaded: () => void): GaugeSection {
  const root = element("section", "gauge-section");
  root.hidden = true;
  const labels = element("div", "gauge-labels");
  const code = element("span", "probe-row-code");
  const name = element("span", "gauge-name");
  const observedLine = element("span", "probe-row-line");
  const observedValue = element("output", "probe-row-value");
  const observedNote = element("span", "probe-row-note");
  observedLine.append(observedValue, observedNote);
  const modelLine = element("span", "probe-row-line");
  const modelValue = element("output", "probe-row-value");
  const modelNote = element("span", "probe-row-note");
  modelLine.append(modelValue, modelNote);
  const source = element("span", "gauge-source");
  labels.append(code, name, observedLine, modelLine, source);
  const plot = element("div", "gauge-plot");
  const canvas = element("canvas", "gauge-chart");
  canvas.setAttribute("aria-hidden", "true");
  canvas.style.height = `${CHART_HEIGHT}px`;
  const peaks = element("ol", "gauge-peaks");
  plot.append(canvas, peaks);
  root.append(labels, plot);

  let current: RiverGauge | null = null;
  let observed: LineState = null;
  let outflow: LineState = null;
  let sequence = 0;

  const read = (loaded: LoadedRiverIndex, gauge: RiverGauge, which: "series" | "outflow", id: number): void => {
    const span = which === "series" ? gauge.series : gauge.outflow;
    if (!span) return;
    const lineId = which === "series" ? gauge.id : `${gauge.id}.outflow`;
    void fetchGaugeSeries(loaded, lineId, span)
      .then((line) => {
        if (id !== sequence) return;
        if (which === "series") observed = line;
        else outflow = line;
        onLoaded();
      })
      .catch((error: unknown) => {
        if (id !== sequence) return;
        if (which === "series") observed = "failed";
        else outflow = "failed";
        console.warn(`river: gauge ${lineId} not read:`, error instanceof Error ? error.message : error);
        onLoaded();
      });
  };

  return {
    root,
    canvas,
    gauge: () => current,
    setGauge(loaded, gauge) {
      sequence += 1;
      current = gauge;
      observed = null;
      outflow = null;
      root.hidden = gauge === null;
      if (!gauge || !loaded) return;
      observed = "pending";
      read(loaded, gauge, "series", sequence);
      if (gauge.outflow) {
        outflow = "pending";
        read(loaded, gauge, "outflow", sequence);
      }
    },
    render(input) {
      const gauge = current;
      if (!gauge) return;
      const unit = unitLabel(gauge.unit);
      code.textContent = `Q · ${unit}`;
      name.textContent = gauge.name;
      name.title = gauge.name;
      source.textContent = `${gauge.source} · ${gauge.id}`;
      const line = typeof observed === "object" && observed !== null ? observed : null;
      const out = typeof outflow === "object" && outflow !== null ? outflow : null;
      const observedNow = valueNear(line, input.time, READOUT_TOLERANCE_MS);
      observedValue.value = observedNow === null ? "--" : formatFlow(observedNow);
      observedNote.textContent =
        observed === "pending" ? t("riverGaugeLoading") : observed === "failed" ? t("riverGaugeUnavailable") : `— ${t("riverObserved")}`;
      const modelledNow = valueNear(input.modelled, input.time, READOUT_TOLERANCE_MS);
      modelValue.value = modelledNow === null ? "--" : formatFlow(modelledNow);
      modelNote.textContent = `╌ ${t("riverModelled")}`;
      // Every reported peak is listed; those in the chart's unit with a time
      // are also marked on it, by the same number.
      const marks: HydrographPeak[] = [];
      peaks.replaceChildren(
        ...gauge.peaks.map((peak, index) => {
          const label = String(index + 1);
          if (peak.timeMs !== undefined && (peak.unit === undefined || peak.unit === gauge.unit)) {
            marks.push({ timeMs: peak.timeMs, value: peak.value, label });
          }
          const item = element("li", "gauge-peak");
          const head = element("span", "gauge-peak-value", `${label} · ${formatFlow(peak.value)} ${unitLabel(peak.unit ?? gauge.unit)}`);
          const parts = [peak.timeMs !== undefined ? input.formatTime(peak.timeMs) : null, peak.note ?? null].filter(
            (part): part is string => part !== null,
          );
          item.append(head, element("span", "gauge-peak-note", parts.join(" · ")));
          return item;
        }),
      );
      peaks.hidden = gauge.peaks.length === 0;
      peaks.setAttribute("aria-label", t("riverPeaks"));
      const data: HydrographData = {
        runTimeMs: input.runTimeMs,
        leadSeconds: input.leadSeconds,
        selected: input.selected,
        observed: line,
        outflow: out,
        modelled: input.modelled,
        peaks: marks,
      };
      const width = canvas.clientWidth;
      if (width === 0) return;
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(CHART_HEIGHT * dpr)) {
        canvas.width = Math.round(width * dpr);
        canvas.height = Math.round(CHART_HEIGHT * dpr);
      }
      const context = canvas.getContext("2d");
      if (!context) return;
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      drawHydrograph(context, { width, height: CHART_HEIGHT }, data, { ink: input.ink, font: FONT });
    },
  };
}
