import "@fontsource/instrument-serif/400.css";
import "@fontsource/instrument-serif/400-italic.css";
import "@fontsource/manrope/500.css";
import "@fontsource/manrope/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./style.css";

import { applyStaticMessages, onLocaleChange, t, type MessageKey } from "./i18n";
import { FORECAST_MODELS, type ForecastModelId } from "./manifest";
import {
  TYPICAL_UPSTREAM_MINUTES,
  buildTimeline,
  formatCycleHour,
  formatHours,
  formatMinutes,
  formatRun,
  formatUtcMinute,
  groupDeadlines,
  metricsWindow,
  orderSources,
  orderedOutcomes,
  parseLatencyMetrics,
  type DeadlineGroup,
  type LatencyMetrics,
  type SourceMetrics,
} from "./metrics/latency";
import { applyPageMeta } from "./pagemeta";
import { mountLanguagePicker } from "./sheet";
import { dataBaseUrl } from "./site";
import { applyTheme, toggleTheme } from "./theme";

/**
 * The run-metrics page.
 *
 * When each forecast run went live, read from `metrics/latency.json`
 * (docs/metrics.md), which the metrics workflow builds from the publish
 * workflows' own records. One card per source with its latency summary
 * and a strip of every arrival over the window, then one table per market
 * deadline: what was live at the cutoff on each day.
 *
 * All times are UTC: cycles are named in UTC and an engineer lining this
 * up against a model's own schedule reads it in UTC. The strips are SVG
 * filled through CSS custom properties, so a theme switch needs no
 * redraw; a locale switch redraws, since every label is copy.
 */

applyStaticMessages();
applyTheme();
applyPageMeta({ path: "/metrics.html" });

document.getElementById("theme-toggle")?.addEventListener("click", () => toggleTheme());

const langTrigger = document.getElementById("lang-toggle");
const langSheet = document.getElementById("lang-sheet");
const langList = document.getElementById("lang-list");
if (langTrigger && langSheet && langList) {
  onLocaleChange(mountLanguagePicker(langTrigger, langSheet, langList));
}

const status = document.getElementById("metrics-status") as HTMLParagraphElement;
const generated = document.getElementById("metrics-generated") as HTMLParagraphElement;
const sourcesSection = document.getElementById("metrics-sources") as HTMLElement;
const deadlinesSection = document.getElementById("metrics-deadlines") as HTMLElement;

const SVG_NS = "http://www.w3.org/2000/svg";
/** The strip's drawing space; the SVG stretches it to the card's width. */
const STRIP_WIDTH = 1000;
const STRIP_HEIGHT = 48;
/** Room under the baseline for the missing-cycle marks. */
const STRIP_FOOT = 8;

type LoadState =
  | { kind: "loading" }
  | { kind: "empty" }
  | { kind: "failed"; message: string }
  | { kind: "ready"; metrics: LatencyMetrics };

let state: LoadState = { kind: "loading" };

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function svg<K extends keyof SVGElementTagNameMap>(tag: K, attributes: Record<string, string | number>): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  return node;
}

function sourceLabel(id: string): string {
  return (FORECAST_MODELS as Partial<Record<string, { label: string }>>)[id]?.label ?? id.toUpperCase();
}

const OUTCOME_KEYS: Readonly<Record<string, MessageKey>> = {
  landed: "metricsOutcomeLanded",
  skipped: "metricsOutcomeSkipped",
  "dry-run": "metricsOutcomeDryRun",
  failed: "metricsOutcomeFailed",
};

function fact(label: string, value: string | Node, options: { title?: string; className?: string } = {}): HTMLDivElement {
  const cell = element("div", options.className);
  cell.append(element("dt", undefined, label));
  const dd = element("dd");
  dd.append(value);
  if (options.title) dd.title = options.title;
  cell.append(dd);
  return cell;
}

function renderTimeline(id: string, source: SourceMetrics, window: readonly [number, number]): HTMLElement {
  const timeline = buildTimeline(source, window);
  const figure = element("figure", "metrics-strip");
  const drawing = svg("svg", {
    viewBox: `0 0 ${STRIP_WIDTH} ${STRIP_HEIGHT + STRIP_FOOT}`,
    preserveAspectRatio: "none",
    role: "img",
    "aria-label": t("metricsTimelineAria", { model: sourceLabel(id) }),
  });
  for (const day of timeline.days) {
    drawing.append(
      svg("line", { class: "metrics-strip-day", x1: day * STRIP_WIDTH, x2: day * STRIP_WIDTH, y1: 0, y2: STRIP_HEIGHT }),
    );
  }
  drawing.append(svg("line", { class: "metrics-strip-base", x1: 0, x2: STRIP_WIDTH, y1: STRIP_HEIGHT, y2: STRIP_HEIGHT }));
  // A bar is most of one cadence step wide, so an hourly source reads as a
  // comb and a twelve-hourly one as distinct posts; never under one unit.
  const stepHours = source.cadenceHours ?? 6;
  const windowHours = Math.max(1, (window[1] - window[0]) / 3_600_000);
  const barWidth = Math.max(1, Math.min(12, (STRIP_WIDTH * stepHours * 0.6) / windowHours));
  for (const bar of timeline.bars) {
    const height = Math.max(1.5, bar.height * (STRIP_HEIGHT - 2));
    const rect = svg("rect", {
      class: "metrics-strip-bar",
      x: bar.x * STRIP_WIDTH - barWidth / 2,
      y: STRIP_HEIGHT - height,
      width: barWidth,
      height,
    });
    const title = svg("title", {});
    title.textContent = `${formatRun(bar.run)} · +${formatMinutes(bar.latencyMinutes)}`;
    rect.append(title);
    drawing.append(rect);
  }
  for (const mark of timeline.missing) {
    const rect = svg("rect", {
      class: "metrics-strip-missing",
      x: mark.x * STRIP_WIDTH - Math.max(barWidth, 3) / 2,
      y: STRIP_HEIGHT + 2,
      width: Math.max(barWidth, 3),
      height: STRIP_FOOT - 2,
    });
    const title = svg("title", {});
    title.textContent = `${formatRun(mark.run)} · ${t("metricsMissing")}`;
    rect.append(title);
    drawing.append(rect);
  }
  figure.append(drawing);

  const axis = element("figcaption", "metrics-strip-axis");
  const start = new Date(window[0]).toISOString().slice(0, 10);
  const end = new Date(window[1]).toISOString().slice(0, 10);
  axis.append(
    element("span", undefined, start),
    element("span", "metrics-strip-scale", `${t("metricsMax")} +${formatMinutes(timeline.scaleMinutes)}`),
    element("span", undefined, end),
  );
  figure.append(axis);
  return figure;
}

function renderSource(id: string, source: SourceMetrics, window: readonly [number, number]): HTMLElement {
  const card = element("article", "metrics-source");
  card.dataset.source = id;

  const head = element("header", "metrics-source-head");
  head.append(element("h3", "metrics-source-name", sourceLabel(id)));
  const meta = element("span", "metrics-source-meta");
  meta.append(element("span", undefined, source.workflow));
  if (source.cadenceHours !== null) meta.append(element("span", undefined, t("metricsCadence", { hours: source.cadenceHours })));
  head.append(meta);
  card.append(head);

  const facts = element("dl", "metrics-facts");
  const latest = source.latest;
  facts.append(
    fact(t("metricsLatest"), latest ? formatRun(latest.run) : "--"),
    fact(t("metricsLanded"), latest ? formatUtcMinute(latest.landedAt) : "--", { title: latest?.landedAt }),
    fact(t("metricsLatency"), latest ? `+${formatMinutes(latest.latencyMinutes)}` : "--"),
    fact(t("metricsMedian"), formatMinutes(source.latencyMinutes?.median)),
    fact("P90", formatMinutes(source.latencyMinutes?.p90)),
    fact(t("metricsMax"), formatMinutes(source.latencyMinutes?.max)),
    fact(
      `${t("metricsTypical")}*`,
      id in TYPICAL_UPSTREAM_MINUTES ? `≈ +${formatMinutes(TYPICAL_UPSTREAM_MINUTES[id])}` : "",
      { className: "is-reference" },
    ),
    fact(t("metricsCycles"), String(source.landed)),
  );

  const missingCount = String(source.missing.length);
  facts.append(
    fact(t("metricsMissing"), missingCount, {
      title: source.missing.map(formatRun).join("\n") || undefined,
      className: source.missing.length > 0 ? "is-warning" : undefined,
    }),
  );

  const outcomes = orderedOutcomes(source.outcomes);
  const outcomeText =
    outcomes
      .map(([name, count]) => `${count} ${OUTCOME_KEYS[name] ? t(OUTCOME_KEYS[name]) : name}`)
      .join(" · ") || "--";
  facts.append(fact(t("metricsRuns"), outcomeText, { className: "is-wide" }));
  card.append(facts);

  if (source.backlog > 0) {
    card.append(element("p", "metrics-backlog", t("metricsBacklog", { count: source.backlog })));
  }

  card.append(renderTimeline(id, source, window));

  if (source.missing.length > 0) {
    const details = element("details", "metrics-missing");
    details.append(element("summary", undefined, `${t("metricsMissingList")} (${source.missing.length})`));
    const list = element("ul");
    for (const run of source.missing) list.append(element("li", undefined, formatRun(run)));
    details.append(list);
    card.append(details);
  }
  return card;
}

function renderDeadline(group: DeadlineGroup): HTMLElement {
  const block = element("section", "metrics-deadline");
  block.dataset.deadline = group.id;
  // The label is the file's own English sentence; the clock and zone
  // beside it are instrument text and say the same in any language.
  block.append(element("h3", "metrics-deadline-name", group.label));
  block.append(element("p", "metrics-deadline-meta", `${group.localTime} ${group.timeZone}`));

  const scroll = element("div", "metrics-table-scroll");
  const table = element("table", "metrics-table");
  const headRow = element("tr");
  headRow.append(element("th", "metrics-table-source", t("metricsSource")), element("th", undefined, t("metricsCommonest")));
  for (const date of group.dates) {
    const cell = element("th", undefined, date.slice(5));
    cell.title = date;
    headRow.append(cell);
  }
  table.append(element("thead"));
  table.tHead!.append(headRow);
  const body = table.appendChild(element("tbody"));
  for (const row of group.rows) {
    const tr = element("tr");
    tr.append(element("th", "metrics-table-source", sourceLabel(row.source)));
    tr.append(
      element(
        "td",
        "metrics-table-common",
        row.commonest ? `${row.commonest.cycle} · ${row.commonest.count}/${row.commonest.of}` : "--",
      ),
    );
    for (const date of group.dates) {
      const day = row.days.get(date);
      const cell = element("td");
      if (!day) {
        cell.textContent = "";
      } else if (!day.run) {
        cell.className = "is-none";
        cell.textContent = t("metricsNoneLive");
        cell.title = `${date} · ${formatUtcMinute(day.cutoff)}`;
      } else {
        cell.append(element("span", "metrics-cycle", formatCycleHour(day.run)), element("span", "metrics-age", formatHours(day.ageHours)));
        cell.title = `${formatRun(day.run)} · ${formatUtcMinute(day.cutoff)}`;
      }
      tr.append(cell);
    }
    body.append(tr);
  }
  scroll.append(table);
  block.append(scroll);
  return block;
}

function say(key: MessageKey | null, params?: Record<string, string | number>, error = false): void {
  status.hidden = key === null;
  status.textContent = key ? t(key, params) : "";
  status.classList.toggle("is-error", error);
}

function render(): void {
  if (state.kind !== "ready") {
    sourcesSection.hidden = true;
    deadlinesSection.hidden = true;
    generated.hidden = true;
    if (state.kind === "loading") say("metricsLoading");
    else if (state.kind === "empty") say("metricsEmpty");
    else say("metricsFailed", { message: state.message }, true);
    return;
  }
  const metrics = state.metrics;
  say(null);
  generated.hidden = false;
  generated.textContent = t("metricsGenerated", {
    time: formatUtcMinute(metrics.generatedAt),
    days: metrics.windowDays,
  });

  const order = orderSources(Object.keys(metrics.sources), Object.keys(FORECAST_MODELS) as ForecastModelId[]);
  const window = metricsWindow(metrics);
  const list = sourcesSection.querySelector<HTMLElement>(".metrics-source-list")!;
  list.replaceChildren(
    ...order.flatMap((id) => {
      const source = metrics.sources[id];
      return source ? [renderSource(id, source, window)] : [];
    }),
  );
  sourcesSection.hidden = false;

  const groups = groupDeadlines(metrics.sources, order);
  const deadlineList = deadlinesSection.querySelector<HTMLElement>(".metrics-deadline-list")!;
  deadlineList.replaceChildren(...groups.map(renderDeadline));
  deadlinesSection.hidden = groups.length === 0;
}

async function load(): Promise<void> {
  render();
  const url = new URL("metrics/latency.json", new URL(dataBaseUrl(), document.baseURI));
  try {
    // The file is rebuilt in place every six hours under one name, so the
    // browser revalidates rather than trusting a cached copy.
    const response = await fetch(url, { cache: "no-cache" });
    if (response.status === 404 || response.status === 403) {
      state = { kind: "empty" };
    } else if (!response.ok) {
      state = { kind: "failed", message: `HTTP ${response.status}` };
    } else if ((response.headers.get("content-type") ?? "").includes("text/html")) {
      // A static host's fallback page for a file it does not have.
      state = { kind: "empty" };
    } else {
      const metrics = parseLatencyMetrics(await response.json());
      state =
        metrics && Object.keys(metrics.sources).length > 0
          ? { kind: "ready", metrics }
          : metrics
            ? { kind: "empty" }
            : { kind: "failed", message: "unsupported metrics schema" };
    }
  } catch (error) {
    state = { kind: "failed", message: error instanceof Error ? error.message : String(error) };
  }
  render();
}

onLocaleChange(() => render());

void load();
