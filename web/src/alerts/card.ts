/** The card a click on a warning area opens: every warning in force at
 * that spot, highest level first, as of the round's minute — a snapshot
 * of now, not of the playhead.
 *
 * Each warning shows its event as the service publishes it (in the
 * service's own language), the product's category and level, when it
 * begins and ends ("until cancelled" where the service states no end),
 * when it was issued, its headline in the reader's language when the
 * service publishes one, else in the first language it does, and the
 * issuing office. The credit lines are each network's own, once per
 * network; for JMA also the boundary set's (docs/alerts.md §9.2): the 出典
 * and 加工 lines are legal text and stay in Japanese, and the survey
 * approval notice folds away; for MeteoAlarm a link to meteoalarm.org and
 * the disclaimer its terms require (docs/alerts.md §10.4), verbatim in
 * English, and the credit of each boundary source its codes were drawn
 * from (§9.3: DWD's warn cells in German, IGN's départements in French). */

import { element } from "../dom";
import { locale, t, type MessageKey } from "../i18n";
import { pickLanguageTag } from "../langtag";
import { alertLevelColor, type AlertHit } from "./layer";
import type { AlertLine, AlertsNetwork } from "./schema";

const CATEGORY_KEYS: Record<string, MessageKey> = {
  rain: "alertCategoryRain",
  flood: "alertCategoryFlood",
  landslide: "alertCategoryLandslide",
  wind: "alertCategoryWind",
  thunderstorm: "alertCategoryThunderstorm",
  "snow-ice": "alertCategorySnowIce",
  heat: "alertCategoryHeat",
  cold: "alertCategoryCold",
  fog: "alertCategoryFog",
  dust: "alertCategoryDust",
  fire: "alertCategoryFire",
  air: "alertCategoryAir",
  coastal: "alertCategoryCoastal",
  marine: "alertCategoryMarine",
  tsunami: "alertCategoryTsunami",
  other: "alertCategoryOther",
};

export function alertCategoryLabel(category: string): string {
  const key = CATEGORY_KEYS[category];
  return key ? t(key) : category;
}

/** The headline the reader gets: their language when the service
 * publishes it, else the first one it does. A service with no headline
 * (none does yet) falls back to the start of its description. */
export function alertHeadline(line: AlertLine, wanted: string): string | null {
  const tags = Object.keys(line.texts);
  const tag = pickLanguageTag(tags, wanted) ?? tags[0];
  if (tag === undefined) return null;
  const texts = line.texts[tag]!;
  if (texts.headline) return texts.headline;
  if (!texts.description) return null;
  const description = texts.description.replace(/\s+/g, " ").trim();
  return description.length > 200 ? `${description.slice(0, 199)}…` : description;
}

/** The boundary set's credit (docs/alerts.md §9.2), shown wherever a map
 * drawn from it is. */
const JMA_SET_SOURCE = "出典：気象庁「予報区等GISデータ」（公共データ利用規約 第1.0版）";
const JMA_SET_EDIT = "加工：PMTiles 化 by xue（気象庁が作成したものではありません）";
const JMA_SET_URL = "https://www.data.jma.go.jp/developer/gis.html";
const JMA_SURVEY_NOTICE =
  "この地図の作成に当たっては、国土地理院長の承認を得て、同院発行の数値地図25000（空間データ基盤）、数値地図（国土基本情報）、電子国土基本図（地図情報）を使用した。（承認番号　平30情使 第401号・令２情使 第125号・令３情使 第90号・令３情使 第123号・令３情使 第648号・令４情使 第437号・令５情使 第71号・令６情使 第382号・令７情使 第395号）";

/** The `cma-areas` credit (docs/alerts.md §9.4): whose outlines they are,
 * and that they place warnings rather than make a survey-approved map. */
export const CMA_SET_CREDIT =
  "边界：高德地图 (Amap), via cnmaps-data; GCJ-02 → WGS84 and tiling by xue · not a survey-approved map (无审图号)";

/** MeteoAlarm's terms (docs/alerts.md §10.4): the credit links to
 * meteoalarm.org, and every redistributor publishes this disclaimer. */
const METEOALARM_URL = "https://meteoalarm.org/en/live/";
const METEOALARM_DISCLAIMER =
  "Time delays between this website and the www.meteoalarm.org website are possible. For the most up-to-date awareness information as published by the participating National Meteorological and Hydrological Services, please refer to www.meteoalarm.org.";

/** The boundary sources of `meteoalarm-areas`, by the code prefix a line
 * names them with (docs/alerts.md §9.3). */
const METEOALARM_SET_CREDITS: readonly { prefix: string; lines: readonly string[] }[] = [
  {
    prefix: "WARNCELLID:",
    lines: [
      "Datenbasis: Deutscher Wetterdienst, Warngebiete als Vektorkacheln aufbereitet",
      "© GeoBasis-DE / BKG 2021 (Daten modifiziert)",
    ],
  },
  {
    prefix: "NUTS3:FR",
    lines: ["IGN – ADMIN EXPRESS COG CARTO, Licence Ouverte 2.0, tuilé par xue"],
  },
];

function meteoalarmCredit(network: AlertsNetwork, lines: readonly AlertLine[]): HTMLElement {
  const credit = element("div", "station-card-credit");
  const source = element("a", "", network.attribution);
  source.href = METEOALARM_URL;
  source.target = "_blank";
  source.rel = "noreferrer";
  credit.append(source, element("div", "", METEOALARM_DISCLAIMER));
  for (const { prefix, lines: text } of METEOALARM_SET_CREDITS) {
    if (lines.some((line) => line.areas.some((code) => code.startsWith(prefix))))
      credit.append(element("div", "alerts-card-set", text.join(" · ")));
  }
  return credit;
}

function jmaSetCredit(): HTMLElement {
  const credit = element("div", "station-card-credit alerts-card-set");
  const source = element("a", "", JMA_SET_SOURCE);
  source.href = JMA_SET_URL;
  source.target = "_blank";
  source.rel = "noreferrer";
  credit.append(source, element("div", "", JMA_SET_EDIT));
  const notice = element("details", "alerts-card-notice");
  notice.append(element("summary", "", "国土地理院長承認"), element("div", "", JMA_SURVEY_NOTICE));
  credit.append(notice);
  return credit;
}

export function buildAlertsCard(
  hits: readonly AlertHit[],
  options: { issued: string; formatTime(time: string): string; darkGround: boolean },
): HTMLElement {
  const root = element("div", "station-card alerts-card");
  const head = element("div", "station-card-head");
  head.append(element("span", "station-card-name", t("alertsCardTitle")));
  head.append(element("span", "station-card-kind", t("alertsInForceNow")));
  root.append(head);
  root.append(
    element("div", "station-card-time", t("alertsAsOf", { time: options.formatTime(options.issued) })),
  );

  const list = element("ol", "alerts-card-list");
  for (const { line, network } of hits) {
    const item = element("li", "alerts-card-item");
    item.style.setProperty("--card-ink", alertLevelColor(line.level, options.darkGround));
    const title = element("div", "alerts-card-title");
    title.append(element("b", "station-card-category", t("alertsLevel", { level: line.level })));
    title.append(element("span", "alerts-card-event", line.native.event));
    item.append(title);
    item.append(element("div", "station-card-kind", alertCategoryLabel(line.category)));
    const from = line.onset ? options.formatTime(line.onset) : null;
    const until = line.expires ? options.formatTime(line.expires) : t("alertsUntilCancelled");
    item.append(element("div", "station-card-time", from ? `${from} → ${until}` : `→ ${until}`));
    item.append(element("div", "station-card-time", t("alertsIssued", { time: options.formatTime(line.sent) })));
    const headline = alertHeadline(line, locale);
    if (headline) item.append(element("div", "alerts-card-headline", headline));
    const issuer = line.sender ? `${line.sender} · ${network.name}` : network.name;
    if (line.url) {
      const link = element("a", "station-card-kind", issuer);
      link.href = line.url;
      link.target = "_blank";
      link.rel = "noreferrer";
      item.append(link);
    } else {
      item.append(element("div", "station-card-kind", issuer));
    }
    list.append(item);
  }
  root.append(list);

  const networks: AlertsNetwork[] = [];
  for (const { network } of hits) if (!networks.includes(network)) networks.push(network);
  for (const network of networks) {
    if (network.id === "meteoalarm") {
      root.append(meteoalarmCredit(network, hits.filter((hit) => hit.network === network).map((hit) => hit.line)));
      continue;
    }
    root.append(element("div", "station-card-credit", network.attribution));
    const drawnBySet = hits.some((hit) => hit.network === network && hit.line.geometry === null);
    if (network.areas?.set.startsWith("jma-") && drawnBySet) root.append(jmaSetCredit());
    if (network.areas?.set === "cma-areas" && drawnBySet) root.append(element("div", "station-card-credit alerts-card-set", CMA_SET_CREDIT));
  }
  return root;
}
