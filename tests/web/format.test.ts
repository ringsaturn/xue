import { describe, expect, it } from "vitest";
import { formatBytes } from "../../web/src/format";

describe("formatBytes", () => {
  it("counts in decimal units on every page", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(999)).toBe("999 B");
    expect(formatBytes(1_000)).toBe("1.0 KB");
    expect(formatBytes(9_950)).toBe("9.9 KB");
    expect(formatBytes(10_000)).toBe("10 KB");
    expect(formatBytes(999_499)).toBe("999 KB");
    expect(formatBytes(1_000_000)).toBe("1.0 MB");
    expect(formatBytes(50_000_000)).toBe("50.0 MB");
  });
});
