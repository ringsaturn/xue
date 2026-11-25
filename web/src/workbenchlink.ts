/** The way from the viewer into the workbench, and what it carries there. */

import type { SectionFill, SectionLine } from "./crosssection/types";
import { shellPath } from "./i18n";
import type { ForecastModelId } from "./manifest";
import { formatLinkTime, linkPoint, searchWithPoint, searchWithSection } from "./urlstate";

export interface WorkbenchPlace {
  line?: SectionLine;
  point?: readonly [number, number];
  /** Valid time, milliseconds. */
  time: number | null;
  fill?: SectionFill;
  top?: number;
}

/** The workbench's defaults (`workbench/state.ts`), left out of a link. */
const DEFAULTS = { model: "gfs", fill: "thetae", top: 100 } as const;
const HOUR_MS = 3_600_000;

export function workbenchUrl(model: ForecastModelId, place: WorkbenchPlace): string {
  const params = new URLSearchParams();
  if (model !== DEFAULTS.model) params.set("model", model);
  const search = searchWithPoint(searchWithSection(params.toString(), place.line ?? null), place.point ? linkPoint(place.point) : null);
  const full = new URLSearchParams(search);
  if (place.time !== null) full.set("time", formatLinkTime(Math.round(place.time / HOUR_MS) * HOUR_MS));
  if (place.fill !== undefined && place.fill !== DEFAULTS.fill) full.set("fill", place.fill);
  if (place.top !== undefined && place.top !== DEFAULTS.top) full.set("top", String(place.top));
  return `${shellPath("/workbench")}?${full.toString().replaceAll("%2C", ",")}`;
}

/** Keep a link's address true to the moment it is used: the playhead moves on
 * under a link drawn once. */
export function followLink(link: HTMLAnchorElement, address: () => string | null): void {
  const set = (): void => {
    const href = address();
    if (href !== null) link.href = href;
  };
  for (const event of ["pointerenter", "pointerdown", "focus", "contextmenu"]) link.addEventListener(event, set);
}
