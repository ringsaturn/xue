/** A showcase case's captions (casecaptions.ts): the caption at a playhead,
 * its line in the locale asked for, and the export's line breaking. */

import { describe, expect, it } from "vitest";

import { captionAt, captionText } from "../../web/src/casecaptions";
import { wrapLines } from "../../web/src/gifexport";
import type { ShowcaseCaption } from "../../web/src/showcase-catalog";

const at = (iso: string) => Date.parse(iso);

const CAPTIONS: ShowcaseCaption[] = [
  { from: at("2026-10-08T00:00:00Z"), to: at("2026-10-08T03:40:00Z"), text: { zh: "真彩色", en: "True colour" } },
  { from: at("2026-10-08T03:40:00Z"), to: at("2026-10-08T10:00:00Z"), text: { zh: "起火", "zh-Hant": "起火了", en: "Ignition" } },
  { from: at("2026-10-09T00:00:00Z"), to: at("2026-10-09T09:00:00Z"), text: { en: "Daytime peak" } },
];

describe("case captions", () => {
  it("finds the caption whose span holds the playhead, from inclusive and to exclusive", () => {
    expect(captionAt(CAPTIONS, at("2026-10-08T00:00:00Z"))).toBe(CAPTIONS[0]);
    expect(captionAt(CAPTIONS, at("2026-10-08T03:40:00Z"))).toBe(CAPTIONS[1]);
    expect(captionAt(CAPTIONS, at("2026-10-08T09:59:59Z"))).toBe(CAPTIONS[1]);
    expect(captionAt(CAPTIONS, at("2026-10-09T08:00:00Z"))).toBe(CAPTIONS[2]);
  });

  it("shows none before, between and after the spans", () => {
    expect(captionAt(CAPTIONS, at("2026-10-07T23:59:59Z"))).toBeNull();
    expect(captionAt(CAPTIONS, at("2026-10-08T10:00:00Z"))).toBeNull();
    expect(captionAt(CAPTIONS, at("2026-10-09T09:00:00Z"))).toBeNull();
    expect(captionAt([], at("2026-10-08T01:00:00Z"))).toBeNull();
    expect(captionText(CAPTIONS, at("2026-10-08T12:00:00Z"), "zh")).toBe("");
  });

  it("speaks the locale asked for, falling back as the case's other text does", () => {
    expect(captionText(CAPTIONS, at("2026-10-08T05:00:00Z"), "zh")).toBe("起火");
    expect(captionText(CAPTIONS, at("2026-10-08T05:00:00Z"), "zh-Hant")).toBe("起火了");
    expect(captionText(CAPTIONS, at("2026-10-08T01:00:00Z"), "zh-Hant")).toBe("真彩色");
    expect(captionText(CAPTIONS, at("2026-10-09T01:00:00Z"), "ja")).toBe("Daytime peak");
  });

  it("breaks an export's line at spaces, or between characters in text without them", () => {
    // One unit of width per character.
    const measure = { measureText: (text: string) => ({ width: text.length }) as TextMetrics };
    expect(wrapLines(measure, "the fire starts at noon", 10)).toEqual(["the fire", "starts at", "noon"]);
    expect(wrapLines(measure, "火场东缘到达国界", 5)).toEqual(["火场东缘到", "达国界"]);
    expect(wrapLines(measure, "short", 10)).toEqual(["short"]);
  });
});
