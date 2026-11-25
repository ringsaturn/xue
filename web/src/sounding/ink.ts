import type { SkewTInk } from "./skewt";

/**
 * The chart's ink, out of the panel's own custom properties.
 *
 * `skewt.ts` takes colors rather than reading a stylesheet, which is what
 * lets it be tested and what lets the dev page hold both themes on screen
 * at once. The panel is the other kind of caller: it has a theme, the theme
 * is in CSS, and the values are read here at every draw so a switch needs
 * no state of its own. The fallbacks are the light theme's, for a context
 * where computed styles are not available.
 */
export function skewTInkFrom(node: Element): SkewTInk {
  const styles = getComputedStyle(node);
  const read = (name: string, fallback: string): string =>
    styles.getPropertyValue(name).trim() || fallback;
  return {
    ink: read("--text", "#1b1a17"),
    inkMuted: read("--muted", "#6e6a60"),
    grid: read("--skewt-grid", "rgba(27, 26, 23, 0.16)"),
    gridStrong: read("--skewt-grid-strong", "rgba(27, 26, 23, 0.45)"),
    paper: read("--skewt-paper", "#f8f5ee"),
    model: read("--skewt-model", "#e8763a"),
    parcel: read("--skewt-parcel", "#6a45a8"),
    temperature: read("--skewt-temperature", "#b4441a"),
    dew: read("--skewt-dew", "#0e7c72"),
    modelDew: read("--skewt-model-dew", "#3bb8a8"),
    dryAdiabat: read("--skewt-dry", "rgba(150, 104, 44, 0.24)"),
    moistAdiabat: read("--skewt-moist", "rgba(44, 104, 168, 0.26)"),
    mixingRatio: read("--skewt-mixing", "rgba(46, 128, 72, 0.4)"),
    tag: read("--skewt-tag", "rgba(27, 26, 23, 0.82)"),
    tagText: read("--skewt-tag-text", "#f3efe6"),
    halo: read("--skewt-halo", "#f8f5ee"),
    buoyant: read("--skewt-buoyant", "rgba(214, 84, 30, 0.2)"),
    inhibited: read("--skewt-inhibited", "rgba(44, 104, 168, 0.2)"),
    moistLayer: read("--skewt-moist-layer", "#0e7c72"),
    inversion: read("--skewt-inversion", "rgba(214, 84, 30, 0.28)"),
  };
}
