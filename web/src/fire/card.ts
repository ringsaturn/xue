/** The card a clicked fire detection opens: what the feature already
 * carries, and nothing fetched. Laid out in the station card's classes so
 * the two read as one family. The sensor name, units and the credit line
 * are instrument text and stay as the product words them. */

import { element } from "../dom";
import { formatPointDegrees } from "../format";
import { t } from "../i18n";
import type { FireDetection } from "./layer";

const CONFIDENCE: Record<string, "fireConfidenceLow" | "fireConfidenceNominal" | "fireConfidenceHigh"> = {
  l: "fireConfidenceLow",
  n: "fireConfidenceNominal",
  h: "fireConfidenceHigh",
};

export function buildFireCard(
  detection: FireDetection,
  options: { formatTime(time: number): string },
): HTMLElement {
  const root = element("div", "station-card");

  const head = element("div", "station-card-head");
  head.append(element("span", "station-card-name", detection.sensor.name));
  head.append(element("span", "station-card-kind", t("fireDetection")));
  root.append(head);

  const when = element("div", "station-card-time");
  when.append(element("span", "", options.formatTime(detection.time)));
  when.append(element("span", "station-card-code", t("fireAcquired")));
  root.append(when);

  const headline = element("div", "station-card-headline");
  headline.append(element("span", "", `${detection.frp.toFixed(1)} MW`));
  headline.title = t("fireRadiativePower");
  root.append(headline);

  const confidence = CONFIDENCE[detection.conf];
  const list = element("dl", "station-card-rows");
  for (const [label, value] of [
    [t("fireBrightness"), `${detection.bt.toFixed(1)} K`],
    [t("fireConfidence"), confidence ? t(confidence) : detection.conf],
    [t("fireOverpass"), detection.dn === "N" ? t("fireNight") : t("fireDay")],
  ] as const) {
    list.append(element("dt", "", label));
    list.append(element("dd", "", value));
  }
  root.append(list);

  root.append(
    element(
      "div",
      "station-card-position",
      `${formatPointDegrees(detection.lat, "NS")} ${formatPointDegrees(detection.lon, "EW")}`,
    ),
  );
  root.append(element("div", "station-card-credit", detection.sensor.attribution));
  return root;
}
