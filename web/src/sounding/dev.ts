/**
 * The skew-T's workbench: the chart on its own, light and dark, with nothing
 * else on the page.
 *
 * `skewt.ts` draws into a context a caller owns, which is what makes it
 * testable and what makes it impossible to look at without a caller. This is
 * the smallest possible one — two canvases, two inks, one real ascent out of
 * the product's golden fixture and one hard-coded model column — so that a
 * change to the barbs or the isohumes can be seen in a second rather than
 * through a run, a pinned point and a probe panel.
 *
 * It is a dev page and only a dev page: `/skewt.html` is not a rollup input
 * (see `vite.config.ts`), not a `STATIC_PAGES` entry in the sitemap
 * (`web/tooling/discovery.ts`) and imported by nothing the shell loads, so it
 * exists under `npm run dev` and in no build output. Open it at
 * `http://localhost:5173/skewt.html`.
 */

import { parcelPath, profileFromModel, profileFromSounding } from "./profile";
import type { ModelLevel, Profile } from "./profile";
import { SAMPLE_STATIONS, type SampleStation } from "./sample";
import { drawSkewT, readoutAt, skewTLayout, toXY } from "./skewt";
import type { SkewTInk, SkewTLayout } from "./skewt";

/** The shell's paper palette, resolved by hand: the chart is handed its ink
 * and never reads a stylesheet, so a dev page can hold both themes on screen
 * at once — which is the point of the page. */
const LIGHT: SkewTInk = {
  ink: "#1b1a17",
  inkMuted: "#6e6a60",
  grid: "rgba(27, 26, 23, 0.20)",
  gridStrong: "rgba(27, 26, 23, 0.45)",
  paper: "#f3efe6",
  model: "#e8763a",
  parcel: "rgba(27, 26, 23, 0.5)",
  temperature: "#b4441a",
  dew: "#0e7c72",
  modelDew: "#3bb8a8",
};

/** And the void palette. */
const DARK: SkewTInk = {
  ink: "#ffffff",
  inkMuted: "rgba(255, 255, 255, 0.62)",
  grid: "rgba(255, 255, 255, 0.20)",
  gridStrong: "rgba(255, 255, 255, 0.5)",
  paper: "#000000",
  model: "#f6a26b",
  parcel: "rgba(255, 255, 255, 0.5)",
  temperature: "#ff8a4c",
  dew: "#3fd6c4",
  modelDew: "#8de6da",
};

/**
 * A coarse model column, hard-coded: eleven isobaric levels of the kind a run
 * publishes (`hgt`/`tmp`/`rh`/`ugrd`/`vgrd` at the eight standard levels plus
 * a few), invented for the page rather than fetched. It is deliberately
 * coarser and smoother than the ascent beside it — that contrast, a
 * fifteen-point model line against a hundred-point sonde, is exactly what the
 * panel will show, and it is what the dotted weight has to survive.
 */
const MODEL_COLUMN: readonly ModelLevel[] = [
  { p: 1000, t: 20.4, rh: 86, z: 92, u: 3.6, v: -2.4 },
  { p: 925, t: 16.8, rh: 80, z: 760, u: 6.8, v: -4.1 },
  { p: 850, t: 12.4, rh: 72, z: 1470, u: 10.2, v: -5.6 },
  { p: 700, t: 2.1, rh: 55, z: 3060, u: 14.1, v: -5.2 },
  { p: 500, t: -14.6, rh: 38, z: 5690, u: 24.8, v: -6.4 },
  { p: 400, t: -25.8, rh: 30, z: 7340, u: 36.2, v: -7.1 },
  { p: 300, t: -41.2, rh: 22, z: 9400, u: 52.4, v: -8.6 },
  { p: 250, t: -49.8, rh: 18, z: 10650, u: 58.1, v: -7.2 },
  { p: 200, t: -55.4, rh: 14, z: 12130, u: 51.3, v: -5.4 },
  { p: 150, t: -58.2, rh: 10, z: 13960, u: 38.7, v: -3.6 },
  { p: 100, t: -60.1, rh: 8, z: 16480, u: 24.2, v: -2.1 },
];

const MARGIN = { left: 34, top: 14, right: 46, bottom: 24 };
const CHART = { width: 460, height: 560 };

function layoutFor(pressureTop: number): SkewTLayout {
  return skewTLayout(
    {
      x: MARGIN.left,
      y: MARGIN.top,
      width: CHART.width - MARGIN.left - MARGIN.right,
      height: CHART.height - MARGIN.top - MARGIN.bottom,
    },
    { pressureTop },
  );
}

interface Panel {
  canvas: HTMLCanvasElement;
  readout: HTMLElement;
  ink: SkewTInk;
}

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (!found) throw new Error(`skewt dev: #${id} is missing`);
  return found as T;
}

/** Paint one panel at the device's own resolution. Scaling the context is the
 * caller's business — this is the caller. */
function paint(panel: Panel, layout: SkewTLayout, observed: Profile, model: Profile): void {
  const ratio = window.devicePixelRatio || 1;
  panel.canvas.width = Math.round(CHART.width * ratio);
  panel.canvas.height = Math.round(CHART.height * ratio);
  panel.canvas.style.width = `${CHART.width}px`;
  panel.canvas.style.height = `${CHART.height}px`;
  const context = panel.canvas.getContext("2d");
  if (!context) throw new Error("skewt dev: no 2d context");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.fillStyle = panel.ink.paper;
  context.fillRect(0, 0, CHART.width, CHART.height);
  drawSkewT(context, layout, panel.ink, {
    profile: observed,
    model,
    parcel: parcelPath(observed),
    font: '10px "IBM Plex Mono", ui-monospace, monospace',
  });
}

function formatReadout(layout: SkewTLayout, observed: Profile, model: Profile, x: number, y: number): string {
  const readout = readoutAt(layout, [observed, model], x, y);
  if (!readout) return "—";
  const number = (value: number | null, digits = 1, unit = ""): string =>
    value === null ? "—" : `${value.toFixed(digits)}${unit}`;
  const line = (name: string, index: number): string => {
    const values = readout.profiles[index]!;
    const wind =
      values.ws === null || values.wd === null
        ? "—"
        : `${Math.round(values.wd).toString().padStart(3, "0")}° ${values.ws.toFixed(1)} m/s`;
    return `${name}  T ${number(values.t, 1, " °C")}  Td ${number(values.td, 1, " °C")}  ${wind}  Z ${number(values.z, 0, " m")}`;
  };
  return [
    `${readout.p.toFixed(0)} hPa  (pointer ${readout.t.toFixed(1)} °C)`,
    line("OBS  ", 0),
    line("MODEL", 1),
  ].join("\n");
}

function start(): void {
  const stationSelect = element<HTMLSelectElement>("station");
  const topSelect = element<HTMLSelectElement>("top");
  const meta = element("meta");
  const panels: Panel[] = [
    { canvas: element<HTMLCanvasElement>("light"), readout: element("light-readout"), ink: LIGHT },
    { canvas: element<HTMLCanvasElement>("dark"), readout: element("dark-readout"), ink: DARK },
  ];

  for (const [index, station] of SAMPLE_STATIONS.entries()) {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = `${station.wmo} — ${station.soundings[0]!.n} levels`;
    stationSelect.append(option);
  }

  const model = profileFromModel(MODEL_COLUMN);
  let layout = layoutFor(100);
  let station: SampleStation = SAMPLE_STATIONS[0]!;
  let observed = profileFromSounding(station.soundings[0]!);

  const render = (): void => {
    const sounding = station.soundings[0]!;
    observed = profileFromSounding(sounding);
    layout = layoutFor(Number(topSelect.value));
    meta.textContent = [
      `${station.id}  ${station.lat}°N ${station.lon}°E  ${station.elev ?? "—"} m`,
      `${sounding.bulletin}  ${sounding.time}  launched ${sounding.launched ?? "—"}`,
      `${sounding.n} of ${sounding.reported} levels  ·  PW ${sounding.derived.pw ?? "—"} mm  ·  0 °C ${
        sounding.derived.freezingLevel ?? "—"
      } gpm  ·  trop ${sounding.derived.tropopause ?? "—"} gpm  ·  850–500 lapse ${
        sounding.derived.lapse850_500 ?? "—"
      } K/km`,
    ].join("\n");
    for (const panel of panels) paint(panel, layout, observed, model);
    showDefaultReadout();
  };

  /** The 500 hPa reading is the one a forecaster names first; it is what the
   * captions hold when nothing is under the pointer. */
  const showDefaultReadout = (): void => {
    const point = toXY(layout, 500, 0);
    for (const panel of panels) {
      panel.readout.textContent = formatReadout(layout, observed, model, point.x, point.y);
    }
  };

  for (const panel of panels) {
    panel.canvas.addEventListener("mousemove", (event) => {
      const rect = panel.canvas.getBoundingClientRect();
      panel.readout.textContent = formatReadout(
        layout,
        observed,
        model,
        event.clientX - rect.left,
        event.clientY - rect.top,
      );
    });
    panel.canvas.addEventListener("mouseleave", showDefaultReadout);
  }

  stationSelect.addEventListener("change", () => {
    station = SAMPLE_STATIONS[Number(stationSelect.value)]!;
    render();
  });
  topSelect.addEventListener("change", render);
  window.addEventListener("resize", render);
  render();
}

start();
