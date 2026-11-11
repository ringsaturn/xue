import { describe, expect, it } from "vitest";

import { setLocale, t } from "../../web/src/i18n";
import { setUnitOverride, setUnitPreset } from "../../web/src/units";

describe("copy that names a quantity", () => {
  it("reads in the viewer's units", () => {
    setLocale("en");
    expect(t("legendPhaseFrostLine")).toBe("Freezing line (0 °C)");
    expect(t("legendFwalert")).toBe("RH under 30 % with gusts of 10 m/s or more");
    expect(t("tcPageErrorsHeading")).toBe("Track error, km");
    setUnitPreset("imperial");
    expect(t("legendPhaseFrostLine")).toBe("Freezing line (32 °F)");
    expect(t("legendFwalert")).toBe("RH under 30 % with gusts of 22 mph or more");
    expect(t("tcPageErrorsHeading")).toBe("Track error, mi");
    setUnitOverride("wind", "kt");
    expect(t("legendFwalert")).toBe("RH under 30 % with gusts of 19 kt or more");
  });

  it("lets a caller's own parameter through", () => {
    setLocale("en");
    expect(t("aqNearest", { distance: "12 km" })).toBe("Nearest air-quality station, 12 km");
  });
});
