import { describe, expect, it } from "vitest";

import { ProbeSeries } from "../../web/src/probe";
import { frameCacheKey, parseFrameCacheKey, variableKey } from "../../web/src/sessionkeys";
import type { BundleMetadata, BundleVariable, LinearQuantization } from "../../web/src/manifest";

const QUANTIZATION: LinearQuantization = {
  type: "linear",
  offset: -60,
  scale: 0.5,
  minimumCode: 0,
  maximumCode: 220,
  nodataCode: 255,
};

/** A file's first variable. Both of the bundles below have one, and both
 * call it 1 — the encoder numbers a bundle's variables 1..n *per file*, so
 * this collision is the ordinary case, not a contrived one. */
function firstVariable(id: string): BundleVariable {
  return { numericId: 1, id, label: id, unit: "", quantization: QUANTIZATION };
}

function grid(): BundleMetadata {
  return {
    schemaVersion: 3,
    model: "GFS",
    runTime: "2026-08-15T06:00:00Z",
    time: { frameCount: 3, unitSeconds: 3600, firstFrameOffset: 0, frameStep: 1 },
    grid: { width: 1440, height: 721 },
    variables: [firstVariable("tmp2m")],
  } as unknown as BundleMetadata;
}

describe("session-scoped keys", () => {
  // The two sessions a `?type=temp&lines=pressure` view holds open: a
  // temperature fill and the pressure lines over it, each the whole of its
  // own file, each calling its only variable 1.
  const fill = { key: 1 };
  const lines = { key: 2 };
  const temperature = firstVariable("tmp2m");
  const pressure = firstVariable("prmsl");

  it("keeps two bundles' variable 1 apart in the frame cache", () => {
    expect(temperature.numericId).toBe(pressure.numericId);
    expect(frameCacheKey(fill, temperature, 6)).not.toBe(frameCacheKey(lines, pressure, 6));

    const cache = new Map<string, string>();
    cache.set(frameCacheKey(fill, temperature, 6), "temperature plane");
    cache.set(frameCacheKey(lines, pressure, 6), "pressure plane");
    expect(cache.size).toBe(2);
    expect(cache.get(frameCacheKey(fill, temperature, 6))).toBe("temperature plane");
    expect(cache.get(frameCacheKey(lines, pressure, 6))).toBe("pressure plane");
  });

  it("keeps each session's frames apart from each other's", () => {
    expect(frameCacheKey(fill, temperature, 6)).not.toBe(frameCacheKey(fill, temperature, 7));
    // Counting one session's cached frames is a prefix scan, so the prefix
    // has to be the session's too.
    const keys = [frameCacheKey(fill, temperature, 6), frameCacheKey(lines, pressure, 6)];
    const mine = keys.filter((key) => key.startsWith(`${variableKey(fill, temperature)}:`));
    expect(mine).toEqual([frameCacheKey(fill, temperature, 6)]);
  });

  it("never reuses a key after a session is torn down and reopened", () => {
    // A new run opens a new session for the same bundle; its planes must not
    // be answered out of the old one's entries.
    const reopened = { key: 3 };
    expect(frameCacheKey(reopened, temperature, 6)).not.toBe(frameCacheKey(fill, temperature, 6));
  });

  it("reads a key back into its three parts", () => {
    expect(parseFrameCacheKey(frameCacheKey(lines, pressure, 24))).toEqual({
      sessionKey: 2,
      numericId: 1,
      frameOffset: 24,
    });
    expect(parseFrameCacheKey("1:1")).toBeNull();
    expect(parseFrameCacheKey("a:b:c")).toBeNull();
  });

  it("keeps two sessions' samples apart in one probe series", () => {
    // One pinned point, several sessions feeding it. Keyed by numericId
    // alone, the pressure bundle's code would be read as the temperature's.
    const metadata = grid();
    const series = new ProbeSeries(116.4, 39.9);
    const cell = series.cellFor(metadata)!;
    const plane = new Uint8Array(1440 * 721);
    plane[cell.index] = 120;
    series.sample(metadata, variableKey(fill, temperature), 0, plane);
    plane[cell.index] = 200;
    series.sample(metadata, variableKey(lines, pressure), 0, plane);
    expect(series.code(variableKey(fill, temperature), 0)).toBe(120);
    expect(series.code(variableKey(lines, pressure), 0)).toBe(200);
  });
});
