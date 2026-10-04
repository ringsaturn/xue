/** Formatting shared by the viewer, the showcase page and the comparison
 * page. Instrument text: the same in every locale. */

function hemisphere(value: number, axis: "NS" | "EW"): string {
  return axis === "NS" ? (value >= 0 ? "N" : "S") : value >= 0 ? "E" : "W";
}

/** A bbox corner, in the compact form the instrument panel uses: whole
 * degrees bare, anything else to a tenth. */
export function formatBboxDegrees(value: number, axis: "NS" | "EW"): string {
  return `${Math.abs(value).toFixed(Math.abs(value) % 1 === 0 ? 0 : 1)}°${hemisphere(value, axis)}`;
}

/** A point, at the precision a grid cell center needs. */
export function formatPointDegrees(value: number, axis: "NS" | "EW"): string {
  return `${Math.abs(value).toFixed(2)}°${hemisphere(value, axis)}`;
}

/** A `[west, south, east, north]` box as its north-west → south-east corners. */
export function formatRegion(bbox: readonly [number, number, number, number]): string {
  const [west, south, east, north] = bbox;
  return `${formatBboxDegrees(north, "NS")} ${formatBboxDegrees(west, "EW")} → ${formatBboxDegrees(south, "NS")} ${formatBboxDegrees(east, "EW")}`;
}

/** Compact UTC stamp to the hour (`2026-09-15 06Z`), `--` for an
 * unparseable one. Cards line several of these up in narrow columns, so
 * they stay in the fixed ISO-like shape rather than a locale long form. */
export function formatUtcHour(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "--";
  const pad = (item: number) => String(item).padStart(2, "0");
  return `${parsed.getUTCFullYear()}-${pad(parsed.getUTCMonth() + 1)}-${pad(parsed.getUTCDate())} ${pad(parsed.getUTCHours())}Z`;
}

/** A byte count in decimal units, as the labels say: 1 KB = 1000 B. Below
 * 10 KB one decimal, so small sizes stay distinguishable. */
export function formatBytes(bytes: number): string {
  if (bytes < 1e3) return `${Math.round(bytes)} B`;
  if (bytes < 1e6) return `${(bytes / 1e3).toFixed(bytes < 1e4 ? 1 : 0)} KB`;
  return `${(bytes / 1e6).toFixed(1)} MB`;
}
