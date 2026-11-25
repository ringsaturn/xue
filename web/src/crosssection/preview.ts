/** What a line being dragged would be drawn as, and what is said of it. */

import { t } from "../i18n";
import { distance, formatDistance, greatCircle } from "../measure";
import type { LinePreview } from "../volumetool";
import { clampLine, MIN_LENGTH, validateLine } from "./line";
import type { LonLat, SectionStore } from "./types";

/**
 * What a drag from `a` to `b` would draw; null for an end off the ground. A
 * line's limit is the store's, so with none open yet (`store` null) the line
 * is not cut. `globe` is whether the map it is dragged on is one.
 */
export function linePreview(a: LonLat | null, b: LonLat | null, store: SectionStore | null, globe: boolean): LinePreview {
  if (!a || !b) return { path: [], label: t("sectionOffMap"), valid: false };
  // Half way round the flat map and more, the shorter way is the other one:
  // the line would leave the pointer.
  if (!globe && Math.abs(b[0] - a[0]) >= 180) return { path: [], label: "", valid: false };
  const refusal = validateLine({ a, b });
  if (refusal === "antipodal") return { path: [], label: t("sectionAntipodal"), valid: false };
  if (refusal) {
    return { path: greatCircle(a, b), label: t("sectionTooShort", { length: formatDistance(MIN_LENGTH) }), valid: false };
  }
  const cut = store ? clampLine({ a, b }, store) : { line: { a, b }, clamped: false };
  const length = formatDistance(distance(cut.line.a, cut.line.b));
  return {
    path: greatCircle(cut.line.a, cut.line.b),
    label: cut.clamped ? t("sectionClamped", { length }) : length,
    valid: true,
  };
}
