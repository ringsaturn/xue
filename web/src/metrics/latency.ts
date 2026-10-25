/** `metrics/latency.json` (docs/metrics.md): its shape, a tolerant reader
 * and the arithmetic the metrics page draws from it. Pure: no DOM, no
 * fetch, so the page's numbers are unit-tested apart from its markup.
 *
 * Everything here is instrument text — run codes, h:mm, UTC stamps — and
 * reads the same in every locale. */

export interface Arrival {
  /** The cycle, `YYYYMMDDHH` (UTC). */
  run: string;
  landedAt: string;
  latencyMinutes: number;
}

export interface DeadlineDay {
  /** The local calendar day, `YYYY-MM-DD`. */
  date: string;
  cutoff: string;
  run: string | null;
  ageHours?: number;
}

export interface Deadline {
  id: string;
  label: string;
  timeZone: string;
  localTime: string;
  days: DeadlineDay[];
}

export interface LatencySummary {
  median: number;
  p90: number;
  max: number;
}

export interface SourceMetrics {
  workflow: string;
  cadenceHours: number | null;
  landed: number;
  missing: string[];
  outcomes: Record<string, number>;
  latencyMinutes: LatencySummary | null;
  latest: Arrival | null;
  deadlines: Deadline[];
  arrivals: Arrival[];
  backlog: number;
}

export interface LatencyMetrics {
  schemaVersion: 1;
  generatedAt: string;
  windowDays: number;
  repository: string;
  sources: Record<string, SourceMetrics>;
}

const HOUR_MS = 3_600_000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function readArrival(value: unknown): Arrival | null {
  if (!isRecord(value)) return null;
  if (typeof value.run !== "string" || runTime(value.run) === null) return null;
  if (typeof value.landedAt !== "string" || !finite(value.latencyMinutes)) return null;
  return { run: value.run, landedAt: value.landedAt, latencyMinutes: value.latencyMinutes };
}

function readDeadline(value: unknown): Deadline | null {
  if (!isRecord(value) || typeof value.id !== "string" || !Array.isArray(value.days)) return null;
  const days: DeadlineDay[] = [];
  for (const day of value.days) {
    if (!isRecord(day) || typeof day.date !== "string" || typeof day.cutoff !== "string") continue;
    const run = typeof day.run === "string" ? day.run : null;
    days.push({ date: day.date, cutoff: day.cutoff, run, ...(run && finite(day.ageHours) ? { ageHours: day.ageHours } : {}) });
  }
  return {
    id: value.id,
    label: typeof value.label === "string" ? value.label : value.id,
    timeZone: typeof value.timeZone === "string" ? value.timeZone : "UTC",
    localTime: typeof value.localTime === "string" ? value.localTime : "",
    days,
  };
}

function readSource(value: unknown): SourceMetrics | null {
  if (!isRecord(value)) return null;
  const outcomes: Record<string, number> = {};
  if (isRecord(value.outcomes)) {
    for (const [name, count] of Object.entries(value.outcomes)) if (finite(count)) outcomes[name] = count;
  }
  const latency = value.latencyMinutes;
  const arrivals = Array.isArray(value.arrivals)
    ? value.arrivals.map(readArrival).filter((item): item is Arrival => item !== null)
    : [];
  return {
    workflow: typeof value.workflow === "string" ? value.workflow : "",
    cadenceHours: finite(value.cadenceHours) ? value.cadenceHours : null,
    landed: finite(value.landed) ? value.landed : arrivals.length,
    missing: Array.isArray(value.missing) ? value.missing.filter((run): run is string => typeof run === "string") : [],
    outcomes,
    latencyMinutes:
      isRecord(latency) && finite(latency.median) && finite(latency.p90) && finite(latency.max)
        ? { median: latency.median, p90: latency.p90, max: latency.max }
        : null,
    latest: readArrival(value.latest),
    deadlines: Array.isArray(value.deadlines)
      ? value.deadlines.map(readDeadline).filter((item): item is Deadline => item !== null)
      : [],
    arrivals,
    backlog: finite(value.backlog) ? value.backlog : 0,
  };
}

/** The file, or null when it is not one this page can read (another
 * schema version, or not an object). Malformed entries inside a readable
 * file are dropped rather than failing the page; `workflowRuns` is the
 * builder's own state and is not read. */
export function parseLatencyMetrics(value: unknown): LatencyMetrics | null {
  if (!isRecord(value) || value.schemaVersion !== 1 || !isRecord(value.sources)) return null;
  if (typeof value.generatedAt !== "string" || Number.isNaN(Date.parse(value.generatedAt))) return null;
  const sources: Record<string, SourceMetrics> = {};
  for (const [id, source] of Object.entries(value.sources)) {
    const read = readSource(source);
    if (read) sources[id] = read;
  }
  return {
    schemaVersion: 1,
    generatedAt: value.generatedAt,
    windowDays: finite(value.windowDays) && value.windowDays > 0 ? value.windowDays : 30,
    repository: typeof value.repository === "string" ? value.repository : "",
    sources,
  };
}

/** A cycle code's instant in epoch ms, null for anything not `YYYYMMDDHH`. */
export function runTime(run: string): number | null {
  const match = /^(\d{4})(\d{2})(\d{2})(\d{2})$/.exec(run);
  if (!match) return null;
  const [year, month, day, hour] = [Number(match[1]), Number(match[2]), Number(match[3]), Number(match[4])];
  if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23) return null;
  return Date.UTC(year, month - 1, day, hour);
}

/** `2026101206` → `2026-10-12 06Z`; anything else as it came. */
export function formatRun(run: string): string {
  const match = /^(\d{4})(\d{2})(\d{2})(\d{2})$/.exec(run);
  return match ? `${match[1]}-${match[2]}-${match[3]} ${match[4]}Z` : run;
}

/** `2026101206` → `06Z`, the cycle hour alone. */
export function formatCycleHour(run: string): string {
  return /^\d{10}$/.test(run) ? `${run.slice(8)}Z` : run;
}

/** An ISO instant as `2026-10-12 11:40Z`, `--` when unparseable. */
export function formatUtcMinute(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "--";
  return `${parsed.toISOString().slice(0, 16).replace("T", " ")}Z`;
}

/** Minutes as `h:mm`, rounded to the minute (`335.2` → `5:35`); `--` for
 * a missing value. Hours are not wrapped at 24: a seasonal run's twelve
 * hours read `11:55`, a two-day backlog `49:10`. */
export function formatMinutes(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || !Number.isFinite(minutes)) return "--";
  const total = Math.max(0, Math.round(minutes));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/** Hours to one decimal, as the deadline table reads an age: `9.0 h`. */
export function formatHours(hours: number | undefined): string {
  return hours === undefined || !Number.isFinite(hours) ? "--" : `${hours.toFixed(1)} h`;
}

/** Typical upstream availability after the cycle time, in minutes: what
 * experience says the model's own data takes to be complete, not anything
 * measured here. Sources without a well-known figure are left out. */
export const TYPICAL_UPSTREAM_MINUTES: Readonly<Record<string, number>> = {
  gfs: 210,
  ecmwf: 420,
  hrrr: 50,
  aifs: 330,
  iconeu: 215,
  arpege: 240,
};

/** Source ids in display order: those `known` lists in its order, then any
 * other id the file carries, alphabetically, so a new source shows up
 * before the page learns its name. */
export function orderSources(ids: readonly string[], known: readonly string[]): string[] {
  const present = new Set(ids);
  const ordered = known.filter((id) => present.has(id));
  const rest = ids.filter((id) => !known.includes(id)).sort();
  return [...ordered, ...rest];
}

/** The outcome names in the order the page lists them; any other name the
 * builder adds later follows, alphabetically. */
export const OUTCOME_ORDER = ["landed", "skipped", "dry-run", "failed"] as const;

/** Nonzero outcome counts, in `OUTCOME_ORDER` then the rest. */
export function orderedOutcomes(outcomes: Record<string, number>): [string, number][] {
  const known: readonly string[] = OUTCOME_ORDER;
  const names = [
    ...OUTCOME_ORDER.filter((name) => name in outcomes),
    ...Object.keys(outcomes)
      .filter((name) => !known.includes(name))
      .sort(),
  ];
  return names.flatMap((name): [string, number][] => {
    const count = outcomes[name];
    return count !== undefined && count > 0 ? [[name, count]] : [];
  });
}

/** The window the summaries cover, `[start, end]` in epoch ms, ending at
 * the file's build time. */
export function metricsWindow(metrics: Pick<LatencyMetrics, "generatedAt" | "windowDays">): [number, number] {
  const end = Date.parse(metrics.generatedAt);
  return [end - metrics.windowDays * 24 * HOUR_MS, end];
}

export interface TimelineBar {
  run: string;
  /** Position along the window, 0..1, at the cycle time. */
  x: number;
  /** Bar height, 0..1, as a share of `scaleMinutes`. */
  height: number;
  latencyMinutes: number;
}

export interface TimelineMark {
  run: string;
  x: number;
}

export interface Timeline {
  bars: TimelineBar[];
  missing: TimelineMark[];
  /** Day boundaries (00 UTC) inside the window, 0..1. */
  days: number[];
  /** What a full-height bar stands for: the largest latency drawn. */
  scaleMinutes: number;
}

/** One source's strip over the window: a bar per arrival at its cycle
 * time, a mark per missing cycle. The scale is the source's own largest
 * latency, because sources differ by an order of magnitude (HRRR within
 * two hours, CFS near twelve) and one shared scale would flatten the fast
 * ones. Arrivals and missing cycles reach back past the window's start
 * (a cycle counts by when it landed, and the gaps between arrivals with
 * it), so anything before the start is pinned to the left edge. */
export function buildTimeline(
  source: Pick<SourceMetrics, "arrivals" | "missing">,
  window: readonly [number, number],
): Timeline {
  const [start, end] = window;
  const span = Math.max(end - start, 1);
  const position = (time: number) => Math.min(1, Math.max(0, (time - start) / span));
  const scaleMinutes = Math.max(1, ...source.arrivals.map((arrival) => arrival.latencyMinutes));
  const bars: TimelineBar[] = [];
  for (const arrival of source.arrivals) {
    const time = runTime(arrival.run);
    if (time === null || time > end) continue;
    bars.push({
      run: arrival.run,
      x: position(time),
      height: Math.max(0, arrival.latencyMinutes) / scaleMinutes,
      latencyMinutes: arrival.latencyMinutes,
    });
  }
  const missing: TimelineMark[] = [];
  for (const run of source.missing) {
    const time = runTime(run);
    if (time !== null && time <= end) missing.push({ run, x: position(time) });
  }
  const days: number[] = [];
  const firstDay = Math.ceil(start / (24 * HOUR_MS)) * 24 * HOUR_MS;
  for (let day = firstDay; day < end; day += 24 * HOUR_MS) if (day > start) days.push(position(day));
  return { bars, missing, days, scaleMinutes };
}

export interface CommonestCycle {
  /** The cycle hour, `06Z`. */
  cycle: string;
  count: number;
  /** Days that had any run live at the cutoff. */
  of: number;
}

/** The cycle hour most often live at a deadline, over the days that had
 * one; a tie goes to the hour live on the most recent of those days,
 * since that is the better guess for tomorrow. Null when no day had a
 * run. */
export function commonestCycle(days: readonly DeadlineDay[]): CommonestCycle | null {
  const counts = new Map<string, { count: number; latest: string }>();
  let of = 0;
  for (const day of days) {
    if (!day.run) continue;
    of += 1;
    const cycle = formatCycleHour(day.run);
    const entry = counts.get(cycle) ?? { count: 0, latest: "" };
    entry.count += 1;
    if (day.date > entry.latest) entry.latest = day.date;
    counts.set(cycle, entry);
  }
  let best: [string, { count: number; latest: string }] | null = null;
  for (const item of counts) {
    if (!best || item[1].count > best[1].count || (item[1].count === best[1].count && item[1].latest > best[1].latest)) {
      best = item;
    }
  }
  return best ? { cycle: best[0], count: best[1].count, of } : null;
}

export interface DeadlineGroup {
  id: string;
  label: string;
  timeZone: string;
  localTime: string;
  /** Every local day any source reports, newest first. */
  dates: string[];
  rows: { source: string; days: Map<string, DeadlineDay>; commonest: CommonestCycle | null }[];
}

/** The deadlines across sources, one group per deadline id: each source
 * carries its own copy of the deadline's definition, and the page draws
 * one table per deadline with a row per source. Sources follow `order`. */
export function groupDeadlines(sources: Readonly<Record<string, SourceMetrics>>, order: readonly string[]): DeadlineGroup[] {
  const groups = new Map<string, DeadlineGroup>();
  for (const id of order) {
    const source = sources[id];
    if (!source) continue;
    for (const deadline of source.deadlines) {
      let group = groups.get(deadline.id);
      if (!group) {
        group = {
          id: deadline.id,
          label: deadline.label,
          timeZone: deadline.timeZone,
          localTime: deadline.localTime,
          dates: [],
          rows: [],
        };
        groups.set(deadline.id, group);
      }
      group.rows.push({
        source: id,
        days: new Map(deadline.days.map((day) => [day.date, day])),
        commonest: commonestCycle(deadline.days),
      });
      for (const day of deadline.days) if (!group.dates.includes(day.date)) group.dates.push(day.date);
    }
  }
  for (const group of groups.values()) group.dates.sort().reverse();
  return [...groups.values()];
}
