import { describe, expect, it } from "vitest";

import type { LogQuantization } from "../../web/src/manifest";
import { compactTick, decodeLog, encodeLog, logCodebookLegend } from "../../web/src/palettes";

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
