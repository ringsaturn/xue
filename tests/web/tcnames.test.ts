import { describe, expect, it } from "vitest";

import { RADII_COLORS, sshsClass } from "../../web/src/tc/intensity";
import { localStormName } from "../../web/src/tc/names";

describe("local storm names", () => {
  it("names a western Pacific storm the way the reader's agency does", () => {
    expect(localStormName("KHANUN", "WP", "zh")).toBe("卡努");
    expect(localStormName("KHANUN", "WP", "ja")).toBe("カーヌン");
    expect(localStormName("KHANUN", "WP", "ko")).toBe("카눈");
    // zh-Hant takes the Central Weather Administration's name where it differs.
    expect(localStormName("ATSANI", "WP", "zh-Hant")).toBe("閃電");
    expect(localStormName("ATSANI", "WP", "zh")).toBe("艾莎尼");
    expect(localStormName("Bang-Lang", "WP", "zh")).toBe("班朗");
  });

  it("keeps the issued name elsewhere and when the table lacks one", () => {
    expect(localStormName("KHANUN", "WP", "en")).toBe("KHANUN");
    expect(localStormName("ERIN", "AL", "zh")).toBe("ERIN");
    expect(localStormName("SENYAR", "WP", "ja")).toBe("SENYAR");
    expect(localStormName("NOT-A-NAME", "WP", "zh")).toBe("NOT-A-NAME");
    expect(localStormName(null, "WP", "zh")).toBeNull();
  });
});

describe("intensity classes", () => {
  it("bins a one-minute wind by the Saffir–Simpson thresholds", () => {
    const kt = (value: number) => value * 0.514444;
    expect(sshsClass(null)).toBeNull();
    expect(sshsClass(kt(30))!.code).toBe("TD");
    expect(sshsClass(kt(34))!.code).toBe("TS");
    expect(sshsClass(kt(64))!.code).toBe("H1");
    expect(sshsClass(kt(96))!.code).toBe("H3");
    expect(sshsClass(kt(140))!.code).toBe("H5");
  });

  it("gives each radii threshold its own color", () => {
    expect(new Set(Object.values(RADII_COLORS)).size).toBe(3);
  });
});
