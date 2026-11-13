/**
 * The observation section of the probe panel: the surface station that
 * speaks for the pin (the one whose marks the meteogram rows carry) and
 * its 24-hour window as charts, on the panel's two columns — the station
 * in the label column, the charts over the capsule's track.
 *
 * The rows only reach back to the run's first frame, so a run issued six
 * hours ago shows six hours of what was measured. The window is the day
 * before the round, whatever the run, and this is where it is read whole.
 *
 * Folded by default, like the ascent below it: a head line names the
 * station, and a press opens the charts. The fold holds across pins.
 *
 * The section owns its DOM only: the probe panel picks the station and
 * reads its line, and hands both in at render time.
 */

import { element } from "../dom";
import { t } from "../i18n";
import { displayZone, formatCompactStamp } from "../timezone";
import { formatDistance, formatHeight, units } from "../units";
import type { SynopNetwork, SynopStation, SynopStationSeries } from "./schema";
import { buildSynopSparklines } from "./sparkline";

export interface ObservationInput {
  station: SynopStation;
  network: SynopNetwork | null;
  distanceKm: number;
  series: SynopStationSeries;
}

export interface ObservationSection {
  root: HTMLElement;
  render(input: ObservationInput | null): void;
}

export function createObservationSection(): ObservationSection {
  const root = element("section", "obs-section");
  root.hidden = true;

  // The head is the skew-T's, so the two folds read as one kind of thing.
  const head = document.createElement("button");
  head.type = "button";
  head.className = "sounding-head";
  head.setAttribute("aria-expanded", "false");
  const headLine = element("span", "sounding-head-line");
  const chevron = element("span", "sounding-chevron");
  chevron.setAttribute("aria-hidden", "true");
  chevron.innerHTML =
    '<svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 3.5L5 6.5l2.5-3" /></svg>';
  head.append(element("span", "sounding-head-code", "OBS 24 H"), headLine, chevron);

  const body = element("div", "obs-body");
  body.hidden = true;
  let open = false;
  head.addEventListener("click", () => {
    open = !open;
    head.setAttribute("aria-expanded", String(open));
    body.hidden = !open;
  });

  const labels = element("div", "obs-labels");
  const name = element("span", "obs-name");
  const where = element("span", "obs-source");
  const source = element("span", "obs-source");
  labels.append(name, where, source);
  const plot = element("div", "obs-plot");
  body.append(labels, plot);
  root.append(head, body);

  // The charts are rebuilt only when what they draw changes: a frame
  // landing repaints the panel, and the window does not move with it.
  let drawn: string | null = null;

  return {
    root,
    render(input) {
      root.hidden = input === null;
      if (!input) {
        drawn = null;
        return;
      }
      const { station, network, distanceKm, series } = input;
      const key = [station.id, series.time.length, series.time[series.time.length - 1], displayZone, JSON.stringify(units)].join("|");
      if (key === drawn) return;
      drawn = key;
      const label = station.name ?? station.id;
      name.textContent = label;
      name.title = label;
      const elevation = station.elev === null ? "" : ` · ${formatHeight(station.elev)}`;
      where.textContent = `${t("stationSynop")} · ${formatDistance(distanceKm, 1)}${elevation}`;
      source.textContent = network ? `${network.name} · ${station.id}` : station.id;
      source.title = network ? `${network.name} · ${network.attribution}` : station.id;
      headLine.textContent = `${label} · ${formatDistance(distanceKm, 1)}`;
      const charts = buildSynopSparklines(series, network?.prPeriod ?? null, (seconds) =>
        formatCompactStamp(seconds * 1000, displayZone),
      );
      if (charts) plot.replaceChildren(charts);
      else plot.replaceChildren();
      root.hidden = charts === null;
    },
  };
}
