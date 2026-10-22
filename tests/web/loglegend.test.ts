import { describe, expect, it } from "vitest";

import type { LogQuantization } from "../../web/src/manifest";
import { compactTick, decodeLog, encodeLog, logCodebookLegend, readHostPalettes } from "../../web/src/palettes";

// A codebook spanning seven decades, as a storage volume in m³ might.
const STORAGE: LogQuantization = {
  type: "log1p",
  trace: 100,
  scale: 1e4,
  maximum: 1e11,
  minimumCode: 1,
  maximumCode: 253,
  zeroCode: 0,
  overflowCode: 254,
  nodataCode: 255,
};

describe("logCodebookLegend", () => {
  it("puts six ticks at codes even along the bar, largest first", () => {
    const ticks = logCodebookLegend(STORAGE);
    expect(ticks).toHaveLength(6);
    expect(ticks[0]).toBe("1e11");
    expect(ticks[5]).toBe("0");
    // The middle tick is the value at the middle code, not half the maximum.
    const middle = Number(ticks[2]);
    expect(encodeLog(STORAGE, middle)).toBeGreaterThan(145);
    expect(encodeLog(STORAGE, middle)).toBeLessThan(160);
    expect(middle).toBeLessThan(STORAGE.maximum / 100);
  });

  it("decodes the codes it labels", () => {
    // Two significant figures: within 5 % of the code's value.
    const tick = Number(logCodebookLegend(STORAGE)[1]);
    expect(Math.abs(tick / decodeLog(STORAGE, 202)! - 1)).toBeLessThan(0.05);
  });
});

describe("compactTick", () => {
  it("prints two significant figures, in exponent form outside 0.01–10000", () => {
    expect(compactTick(0)).toBe("0");
    expect(compactTick(23_456_000_000)).toBe("2.3e10");
    expect(compactTick(1e11)).toBe("1e11");
    expect(compactTick(0.005)).toBe("5e-3");
    expect(compactTick(620.4)).toBe("620");
    expect(compactTick(1.234)).toBe("1.2");
  });
});

describe("readHostPalettes", () => {
  it("keeps well-formed palettes and drops malformed ones whole", () => {
    const palettes = readHostPalettes({
      outflw: [[1, 68, 1, 84, 255], [5e4, 253, 231, 37, 255]],
      unordered: [[10, 0, 0, 0, 255], [1, 0, 0, 0, 255]],
      short: [[1, 0, 0, 0, 255]],
      channel: [[1, 0, 0, 0, 255], [2, 300, 0, 0, 255]],
    });
    expect([...palettes.keys()]).toEqual(["outflw"]);
    expect(palettes.get("outflw")!.stops[1]).toEqual([5e4, 253, 231, 37, 255]);
    expect(palettes.get("outflw")!.sampling).toBeNull();
    expect(readHostPalettes(null).size).toBe(0);
    expect(readHostPalettes([1, 2]).size).toBe(0);
  });
});

describe("readHostPalettes with a sampling", () => {
  it("takes { stops, sampling } and refuses an unknown sampling or key", () => {
    const stops = [[0.01, 15, 15, 40, 110], [30, 62, 200, 222, 255]];
    const palettes = readHostPalettes({
      rivdph: { stops, sampling: "nearest" },
      bad: { stops, sampling: "classes" },
      extra: { stops, colour: "blue" },
    });
    expect([...palettes.keys()]).toEqual(["rivdph"]);
    expect(palettes.get("rivdph")!.sampling).toBe("nearest");
  });
});

describe("logCodebookLegend over part of the bar", () => {
  it("ticks the codes between the bounds it is given", () => {
    const from = encodeLog(STORAGE, 1e4);
    const to = encodeLog(STORAGE, 1e10);
    const ticks = logCodebookLegend(STORAGE, from, to);
    expect(Math.abs(Number(ticks[0]) / 1e10 - 1)).toBeLessThan(0.05);
    expect(Math.abs(Number(ticks[5]) / 1e4 - 1)).toBeLessThan(0.05);
  });
});
