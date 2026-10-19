/** A showcase case's markers (casemarkers.ts): the features the layer
 * draws, the dimming before an event's time, the antimeridian box test and
 * the card a marker opens. */

import { describe, expect, it } from "vitest";

import {
  buildMarkerCard,
  MARKER_DIM_OPACITY,
  markerFeatures,
  markerOpacityExpression,
  markerReached,
} from "../../web/src/casemarkers";
import { boundsContain, type ShowcaseMarker } from "../../web/src/showcase-catalog";

const IGNITION: ShowcaseMarker = {
  id: "ignition",
  kind: "event",
  lon: 119.395,
  lat: 46.883,
  time: Date.parse("2026-10-08T03:46:00Z"),
  label: { zh: "起火点", "zh-Hant": "起火點", en: "Ignition" },
  note: { zh: "说明", en: "Where it started" },
  sources: [{ title: "NEMA bulletin", url: "https://nema.gov.mn/post/180594" }],
};

const PORT: ShowcaseMarker = {
  id: "port",
  kind: "place",
  lon: 119.4644,
  lat: 47.3251,
  label: { en: "Border crossing" },
  note: { en: "A crossing" },
  sources: [],
};

describe("case markers", () => {
  it("draws one labelled feature per marker, in the locale asked for", () => {
    const features = markerFeatures([IGNITION, PORT], "zh-Hant");
    expect(features.features.map((feature) => feature.geometry.coordinates)).toEqual([
      [119.395, 46.883],
      [119.4644, 47.3251],
    ]);
    expect(features.features.map((feature) => feature.properties)).toEqual([
      { id: "ignition", kind: "event", label: "起火點", time: IGNITION.time },
      // No zh-Hant, no zh: English; no time: 0, which every playhead is past.
      { id: "port", kind: "place", label: "Border crossing", time: 0 },
    ]);
  });

  it("holds an event faint until the playhead reaches it", () => {
    const time = IGNITION.time!;
    expect(markerReached(IGNITION, time - 1)).toBe(false);
    expect(markerReached(IGNITION, time)).toBe(true);
    expect(markerReached(PORT, 0)).toBe(true);
    expect(markerOpacityExpression(time)).toEqual(["case", ["<=", ["number", ["get", "time"]], time], 1, MARKER_DIM_OPACITY]);
  });

  it("tests a point against a box, across the antimeridian too", () => {
    expect(boundsContain([117, 45.5, 122, 48.5], 119.395, 46.883)).toBe(true);
    expect(boundsContain([117, 45.5, 122, 48.5], 123, 46.883)).toBe(false);
    expect(boundsContain([117, 45.5, 122, 48.5], 119.395, 49)).toBe(false);
    expect(boundsContain([170, -50, -170, -30], 175, -40)).toBe(true);
    expect(boundsContain([170, -50, -170, -30], -175, -40)).toBe(true);
    expect(boundsContain([170, -50, -170, -30], 0, -40)).toBe(false);
  });

  it("opens a card with the note, the time and the sources as safe links", () => {
    const card = buildMarkerCard(IGNITION, { locale: "en", formatTime: (time) => new Date(time).toISOString() });
    expect(card.getAttribute("aria-label")).toBe("Ignition");
    expect(card.querySelector(".marker-card-note")?.textContent).toBe("Where it started");
    expect(card.querySelector("time")?.getAttribute("datetime")).toBe("2026-10-08T03:46:00.000Z");
    const link = card.querySelector("a")!;
    expect(link.href).toBe("https://nema.gov.mn/post/180594");
    expect(link.textContent).toBe("NEMA bulletin");
    expect(link.rel).toBe("noopener noreferrer");
    expect(link.target).toBe("_blank");
    // A place has no time and, here, no sources.
    const place = buildMarkerCard(PORT, { locale: "en", formatTime: String });
    expect(place.querySelector("time")).toBeNull();
    expect(place.querySelector("a")).toBeNull();
  });
});
