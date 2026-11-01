/** The card a clicked air-quality station opens, in the station card's
 * classes: the station's current values from its network's stations file,
 * each with the unit the index names, the agency that owns the data and
 * the network's credit line — both of which the networks' terms ask to be
 * shown with the values — and, for a network publishing unvalidated data,
 * a line saying so. The station's 24-hour window is read once the card is
 * open and adds the day's PM2.5 range and the network's own spelling of
 * the name in the reader's language.
 *
 * No index is computed or named: the product publishes concentrations
 * (docs/aq.md §2). Element names (`PM2.5`, `O₃`) and units are instrument
 * text and stay as they are. */

import { element } from "../dom";
import { formatPointDegrees } from "../format";
import { locale, t } from "../i18n";
import { pickLanguageTag } from "../langtag";
import { pm25Color } from "../stations/layers";
import { aqUnit, type AqIndex, type AqNetwork, type AqStation, type AqStationSeries } from "./schema";

const ELEMENT_LABELS: Record<string, string> = {
  pm25: "PM2.5",
  pm10: "PM10",
  o3: "O₃",
  no2: "NO₂",
  so2: "SO₂",
  co: "CO",
};

export function aqElementLabel(key: string): string {
  return ELEMENT_LABELS[key] ?? key.toUpperCase();
}

/** A value to the precision §2 publishes it at: CO to a hundredth, the
 * rest to a tenth. */
export function formatAqValue(key: string, value: number): string {
  return value.toFixed(key === "co" ? 2 : 1);
}

export interface AqCard {
  root: HTMLElement;
  /** Add what the station's window holds, once it has been read. */
  setSeries(series: AqStationSeries): void;
}

export function buildAqCard(
  station: AqStation,
  network: AqNetwork,
  index: AqIndex,
  options: { formatTime(time: string): string; onPin?: () => void },
): AqCard {
  const root = element("div", "station-card");
  const pm25 = station.values.pm25 ?? null;
  const ink = pm25Color(pm25);
  if (ink) root.style.setProperty("--card-ink", ink);

  const head = element("div", "station-card-head");
  const name = element("span", "station-card-name", station.name ?? station.id);
  head.append(name);
  head.append(element("span", "station-card-kind", t("aqStation")));
  root.append(head);

  const when = element("div", "station-card-time");
  when.append(element("span", "", options.formatTime(station.obsTime)));
  when.append(element("span", "station-card-code", t("stationObserved")));
  root.append(when);

  if (pm25 !== null) {
    const headline = element("div", "station-card-headline");
    headline.append(element("b", "station-card-category", "PM2.5"));
    headline.append(element("span", "", `${formatAqValue("pm25", pm25)} ${aqUnit(index, "pm25")}`));
    root.append(headline);
  }

  const list = element("dl", "station-card-rows");
  for (const key of index.elements) {
    const value = station.values[key];
    if (value === null || value === undefined) continue;
    list.append(element("dt", "", aqElementLabel(key)));
    list.append(element("dd", "", `${formatAqValue(key, value)} ${aqUnit(index, key)}`.trimEnd()));
  }
  if (station.agency) {
    list.append(element("dt", "", t("aqAgency")));
    list.append(element("dd", "", station.agency));
  }
  list.append(element("dt", "", t("aqNetwork")));
  list.append(element("dd", "", network.name));
  root.append(list);

  // Filled in from the window: the day's PM2.5 range, a row of its own.
  const range = element("dl", "station-card-rows");
  range.hidden = true;
  root.append(range);

  if (network.preliminary) root.append(element("div", "station-card-note", t("aqPreliminary")));
  root.append(
    element(
      "div",
      "station-card-position",
      `${formatPointDegrees(station.lat, "NS")} ${formatPointDegrees(station.lon, "EW")}`,
    ),
  );
  root.append(element("div", "station-card-credit", `${network.name} · ${network.attribution}`));
  if (options.onPin) {
    const button = element("button", "station-card-action", t("stationPinHere"));
    button.type = "button";
    const onPin = options.onPin;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      onPin();
    });
    root.append(button);
  }

  return {
    root,
    setSeries(series) {
      if (series.names) {
        const tag = pickLanguageTag(Object.keys(series.names), locale);
        if (tag) name.textContent = series.names[tag]!;
      }
      const values = (series.obs.pm25 ?? []).filter((value): value is number => value !== null);
      if (values.length < 2) return;
      const unit = aqUnit(index, "pm25");
      range.replaceChildren(
        element("dt", "", t("aqDayRange")),
        element(
          "dd",
          "",
          `${formatAqValue("pm25", Math.min(...values))}–${formatAqValue("pm25", Math.max(...values))} ${unit}`,
        ),
      );
      range.hidden = false;
    },
  };
}
