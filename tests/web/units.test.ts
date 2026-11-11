import { describe, expect, it, vi } from "vitest";

import type { BundleVariable } from "../../web/src/manifest";
import {
  browserRegion,
  convertDistance,
  convertFileLegendLabel,
  convertHeight,
  convertLegendLabel,
  convertPrecipitation,
  convertPressure,
  convertTemperature,
  convertTemperatureDelta,
  convertWind,
  displayUnit,
  displayValue,
  formatDistance,
  formatPressure,
  formatTemperature,
  formatUnitPrefs,
  formatValueReadout,
  formatWind,
  formatWindKnots,
  IMPERIAL_UNITS,
  METRIC_UNITS,
  onUnitsChange,
  parseUnitPrefs,
  regionUnits,
  resolveUnits,
  setUnitOverride,
  setUnitPrefs,
  setUnitPreset,
  unitConverts,
  unitPrefs,
  units,
  variableDisplayUnit,
  variableDisplayValue,
} from "../../web/src/units";

/** 500 hPa geopotential height and 850 hPa θe, as the encoders write them. */
const HGT500 = { discipline: 0, parameterCategory: 3, parameterNumber: 5, typeOfFirstFixedSurface: 100 };
const THETAE850 = { discipline: 0, parameterCategory: 0, parameterNumber: 3, typeOfFirstFixedSurface: 100 };
/** The model terrain: the same parameter as the height, on the ground. */
const OROG = { discipline: 0, parameterCategory: 3, parameterNumber: 5, typeOfFirstFixedSurface: 1 };

describe("auto units by region", () => {
  it("reads the region off the browser's languages, filling a bare language", () => {
    expect(browserRegion(["ja"])).toBe("JP");
    expect(browserRegion(["en-US"])).toBe("US");
    expect(browserRegion(["en-GB"])).toBe("GB");
    expect(browserRegion(["fr-CA"])).toBe("CA");
    expect(browserRegion(["de-DE"])).toBe("DE");
    expect(browserRegion(["not a tag", "de-DE"])).toBe("DE");
    expect(browserRegion([])).toBeNull();
    // A bare multi-region language names no region; a later tag that does wins.
    expect(browserRegion(["en", "zh-CN", "zh", "ja"])).toBe("CN");
    expect(browserRegion(["en", "ja"])).toBe("JP");
    expect(browserRegion(["en"])).toBeNull();
  });

  it("gives each region its weather service's units", () => {
    expect(regionUnits("JP")).toEqual(METRIC_UNITS);
    expect(regionUnits("US")).toEqual(IMPERIAL_UNITS);
    expect(regionUnits("GB")).toEqual({ ...METRIC_UNITS, wind: "mph", distance: "mi" });
    expect(regionUnits("CA")).toEqual({ ...METRIC_UNITS, wind: "kmh", pressure: "kPa" });
    expect(regionUnits("DE")).toEqual({ ...METRIC_UNITS, wind: "kmh" });
    expect(regionUnits(null)).toEqual(METRIC_UNITS);
  });

  it("lays the overrides over the preset", () => {
    expect(resolveUnits({ preset: "auto", overrides: { wind: "kt" } }, "JP")).toEqual({ ...METRIC_UNITS, wind: "kt" });
    expect(resolveUnits({ preset: "metric", overrides: {} }, "US")).toEqual(METRIC_UNITS);
    expect(resolveUnits({ preset: "imperial", overrides: { temperature: "C" } }, "JP")).toEqual({ ...IMPERIAL_UNITS, temperature: "C" });
  });
});

describe("unit preferences", () => {
  it("parses and formats the stored choice", () => {
    for (const text of ["auto", "metric", "imperial", "metric,kt", "imperial,c,hpa,mm", "auto,f,kmh,inhg,in,ft,nmi"]) {
      expect(formatUnitPrefs(parseUnitPrefs(text))).toBe(text);
    }
    expect(parseUnitPrefs("metric,kt")).toEqual({ preset: "metric", overrides: { wind: "kt" } });
    expect(parseUnitPrefs("F, KMH")).toEqual({ preset: "auto", overrides: { temperature: "F", wind: "kmh" } });
    expect(parseUnitPrefs("bogus")).toEqual({ preset: "auto", overrides: {} });
    expect(parseUnitPrefs(null)).toEqual({ preset: "auto", overrides: {} });
  });

  it("drops an override that matches the preset and tells the listeners", () => {
    const listener = vi.fn();
    onUnitsChange(listener);
    setUnitPreset("metric");
    setUnitOverride("wind", "kt");
    expect(units.wind).toBe("kt");
    expect(unitPrefs.overrides).toEqual({ wind: "kt" });
    setUnitOverride("wind", "ms");
    expect(unitPrefs.overrides).toEqual({});
    setUnitOverride("pressure", "inHg");
    setUnitOverride("pressure", null);
    expect(units.pressure).toBe("hPa");
    expect(listener).toHaveBeenCalledTimes(5);
  });
});

describe("conversions", () => {
  it("converts from the canonical units", () => {
    expect(convertTemperature(100, "F")).toBeCloseTo(212, 9);
    expect(convertTemperature(-40, "F")).toBeCloseTo(-40, 9);
    expect(convertTemperatureDelta(10, "F")).toBeCloseTo(18, 9);
    expect(convertWind(10, "kmh")).toBeCloseTo(36, 9);
    expect(convertWind(1852 / 3600, "kt")).toBeCloseTo(1, 9);
    expect(convertWind(1609.344 / 3600, "mph")).toBeCloseTo(1, 9);
    expect(convertPressure(1013.25, "inHg")).toBeCloseTo(29.921, 3);
    expect(convertPressure(1013.25, "kPa")).toBeCloseTo(101.325, 9);
    expect(convertPressure(1013.25, "mmHg")).toBeCloseTo(760, 2);
    expect(convertPrecipitation(25.4, "in")).toBeCloseTo(1, 9);
    expect(convertHeight(0.3048, "ft")).toBeCloseTo(1, 9);
    expect(convertDistance(1.609344, "mi")).toBeCloseTo(1, 9);
    expect(convertDistance(1.852, "nmi")).toBeCloseTo(1, 9);
  });

  it("shows a file unit in the viewer's unit", () => {
    expect(displayUnit("K")).toBe("°C");
    expect(displayValue("K", 223.15)).toBeCloseTo(-50, 9);
    expect(unitConverts("°C")).toBe(false);
    setUnitPreset("imperial");
    expect(displayUnit("K")).toBe("°F");
    expect(displayValue("K", 273.15)).toBeCloseTo(32, 9);
    expect(displayUnit("°C")).toBe("°F");
    expect(displayUnit("m/s")).toBe("mph");
    expect(displayUnit("hPa")).toBe("inHg");
    expect(displayUnit("mm")).toBe("in");
    expect(displayUnit("mm/h")).toBe("in/h");
    expect(displayValue("mm/h", 25.4)).toBeCloseTo(1, 9);
    expect(displayUnit("km")).toBe("mi");
    expect(displayUnit("dBZ")).toBe("dBZ");
    expect(displayUnit("1")).toBe("");
    expect(unitConverts("°C")).toBe(true);
  });

  it("keeps a geopotential height in metres and a θe in kelvin", () => {
    setUnitPreset("imperial");
    expect(displayUnit("m", HGT500)).toBe("m");
    expect(displayValue("m", 5640, HGT500)).toBe(5640);
    expect(displayUnit("m", OROG)).toBe("ft");
    expect(displayValue("m", 0.3048, OROG)).toBeCloseTo(1, 9);
    expect(displayUnit("K", THETAE850)).toBe("K");
    expect(displayValue("K", 330, THETAE850)).toBe(330);
    setUnitPreset("metric");
    expect(displayUnit("K", THETAE850)).toBe("K");
    const variable = { unit: "K", parameter: { ...THETAE850, scaleFactorOfFirstFixedSurface: 0, scaledValueOfFirstFixedSurface: 85000 } };
    expect(variableDisplayUnit(variable)).toBe("K");
    expect(variableDisplayValue(variable, 330)).toBe(330);
  });

  it("prints a readout with the digits the display unit is worth", () => {
    const precipitation = {
      unit: "mm/h",
      quantization: { type: "linear", offset: 0, scale: 0.1, minimumCode: 0, maximumCode: 254, nodataCode: 255 },
    } as unknown as BundleVariable;
    expect(formatValueReadout(precipitation, 1.2)).toBe("1.2");
    setUnitPreset("imperial");
    expect(formatValueReadout(precipitation, displayValue("mm/h", 1.2))).toBe("0.05");
  });
});

describe("legend relabelling", () => {
  it("leaves a legend alone in its own unit", () => {
    expect(convertLegendLabel("°C", "20")).toBe("20");
    expect(convertLegendLabel("K", "-30")).toBe("-30");
    expect(convertLegendLabel("dBZ", "45")).toBe("45");
  });

  it("relabels a tick at the same place on the bar", () => {
    setUnitPreset("imperial");
    expect(convertLegendLabel("°C", "0")).toBe("32");
    expect(convertLegendLabel("°C", "-40")).toBe("-40");
    expect(convertLegendLabel("°C", "−10")).toBe("14");
    expect(convertLegendLabel("°C", "−40")).toBe("−40");
    // A kelvin field's hand-written legend is ticked in °C.
    expect(convertLegendLabel("K", "-30")).toBe("-22");
    expect(convertLegendLabel("hPa", "1013")).toBe("29.91");
    expect(convertLegendLabel("mm/h", "0.5")).toBe("0.02");
    expect(convertLegendLabel("m", "8000", OROG)).toBe("26247");
    expect(convertLegendLabel("m", "5640", HGT500)).toBe("5640");
    expect(convertLegendLabel("K", "330", THETAE850)).toBe("330");
    expect(convertLegendLabel("m/s", "+30")).toBe("+67");
    expect(convertLegendLabel("m/s", "≥40")).toBe("≥89");
    setUnitOverride("wind", "kt");
    expect(convertLegendLabel("m/s", "40")).toBe("78");
  });

  it("relabels a tick written in the file's own unit", () => {
    expect(convertFileLegendLabel("K", "273.15")).toBe("0");
    expect(convertFileLegendLabel("K", "300", THETAE850)).toBe("300");
    setUnitPreset("imperial");
    expect(convertFileLegendLabel("K", "273.15")).toBe("32");
    expect(convertFileLegendLabel("mm", "25.4")).toBe("1");
  });
});

describe("point readouts", () => {
  it("writes a value and its unit symbol", () => {
    expect(formatTemperature(20)).toBe("20.0 °C");
    expect(formatWind(10)).toBe("10.0 m/s");
    expect(formatPressure(1013.2)).toBe("1013.2 hPa");
    expect(formatDistance(10, 0)).toBe("10 km");
    expect(formatWindKnots(55)).toBe("28 m/s");
    setUnitPrefs({ preset: "imperial", overrides: {} });
    expect(formatTemperature(20)).toBe("68.0 °F");
    expect(formatWind(10)).toBe("22 mph");
    expect(formatPressure(1013.25)).toBe("29.92 inHg");
    expect(formatDistance(10, 0)).toBe("6 mi");
    setUnitOverride("wind", "kt");
    expect(formatWindKnots(55)).toBe("55 kt");
  });
});
