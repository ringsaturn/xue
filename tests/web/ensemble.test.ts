import { describe, expect, it } from "vitest";

import { identifyBundle, identityForBundleId, identityForParameter, registeredBundleId, sameChart, sameIdentity } from "../../web/src/identity";
import { FAMILIES, familyLevels, familyOf, levelCode, probabilityLabel, scalarLegendRange } from "../../web/src/levels";
import {
  ENSEMBLE_IDS,
  FORECAST_MODELS,
  KNOWN_BUNDLE_IDS,
  frameOffsets,
  parseBundleMetadata,
  type BundleEnsemble,
  type BundleVariable,
  type LinearQuantization,
} from "../../web/src/manifest";
import { meteogramRowBundles, meteogramRows } from "../../web/src/meteogram";
import { buildPalette } from "../../web/src/palettes";
import { statisticKey, statisticNote } from "../../web/src/statistics";
import { statisticBase, statisticMembers, variableSpec } from "../../web/src/variables";
import { metadataJson, publishedBundleIds, registryEntries, registryVariable, rgba } from "./helpers";

/** The registry both encoders build from (`rust/xue/registry.json`): every
 * published variable carrying an `ensemble` block. */
const registry = registryEntries<LinearQuantization>(ENSEMBLE_IDS);

function bundleVariable(id: string): BundleVariable {
  const entry = registry[id]!;
  return registryVariable(id, entry, entry.quality, { ensemble: entry.ensemble });
}

/** A variable of a quantity's own registry entry with an `ensemble` block
 * the source writes on it: the median an ensemble publishes under the
 * quantity's id. */
function centreVariable(id: string, ensemble: BundleEnsemble): BundleVariable {
  const entry = registryEntries<LinearQuantization>([id])[id]!;
  return registryVariable(id, entry, entry.quality, { ensemble });
}

function metadata(variable: BundleVariable, firstFrameOffset = 0, schemaVersion = 3): string {
  const json = JSON.parse(
    metadataJson(
      {
        model: "CMA-GEPS",
        runTime: "2026-11-11T12:00:00Z",
        time: { unitSeconds: 3600, firstFrameOffset, frameCount: 3, frameStep: 6 },
        grid: { width: 1440, height: 720, firstLongitude: 0, firstLatitude: 89.875, longitudeStep: 0.25, latitudeStep: -0.25, wrapLongitude: true },
      },
      [variable],
    ),
  ) as { schemaVersion: number };
  json.schemaVersion = schemaVersion;
  return JSON.stringify(json);
}

/** What cmageps publishes: the centres under their own ids and every
 * statistic beside them. */
const CMAGEPS = new Set(["tmp2m", "tmp850", "prate", "gust", "tcdc", "cape", "prmsl", "hgt500", ...ENSEMBLE_IDS]);
const cmageps = (id: string) => CMAGEPS.has(id);

const QUARTILE_BASES = ["tmp2m", "tmp850", "gust", "tcdc", "cape", "prate"] as const;
const THRESHOLDS = [1, 5, 10, 25, 50, 100] as const;

describe("the ensemble registry", () => {
  it("names the twenty published statistics, each with a row", () => {
    const expected = [
      ...QUARTILE_BASES.flatMap((base) => [`${base}q25`, `${base}q75`]),
      "prmslsd",
      "hgt500sd",
      ...THRESHOLDS.map((threshold) => `apcp24gt${threshold}`),
    ];
    expect([...ENSEMBLE_IDS].sort()).toEqual(expected.sort());
    const published = publishedBundleIds();
    for (const id of ENSEMBLE_IDS) {
      expect(KNOWN_BUNDLE_IDS).toContain(id);
      expect(published.has(id), id).toBe(true);
      expect(variableSpec(id), id).not.toBeNull();
    }
    // The input-only quartiles of the six-hour total reach no bundle.
    expect(ENSEMBLE_IDS).not.toContain("apcpq25");
  });

  it("keys each statistic the way the block says it", () => {
    expect(statisticKey({ derivedForecast: 0 })).toBe("mean");
    expect(statisticKey({ derivedForecast: 4 })).toBe("spread");
    expect(statisticKey({ derivedForecast: 1 })).toBe("d1");
    expect(statisticKey({ percentileValue: 75 })).toBe("q75");
    expect(statisticKey(registry.apcp24gt10!.ensemble!)).toBe("p3:10:");
    expect(statisticKey(registry.apcp24gt1!.ensemble!)).toBe("p3:1:");
  });
});

describe("the ensemble block", () => {
  it("parses each of the three shapes", () => {
    for (const id of ["tmp2mq75", "prmslsd", "apcp24gt10"]) {
      const parsed = parseBundleMetadata(metadata(bundleVariable(id)));
      expect(parsed.variables[0]!.ensemble).toEqual(registry[id]!.ensemble);
    }
    expect(parseBundleMetadata(metadata(centreVariable("tmp2m", { percentileValue: 50 }))).variables[0]!.ensemble).toEqual({
      percentileValue: 50,
    });
  });

  it("rejects a block of no shape, out of range, or with the wrong limits for its type", () => {
    const percentile = bundleVariable("tmp2mq75");
    const probability = bundleVariable("apcp24gt10");
    const withBlock = (variable: BundleVariable, block: unknown) =>
      metadata({ ...variable, ensemble: block as BundleEnsemble });
    // Never null, never a non-object, never two shapes at once or a stray key.
    expect(() => parseBundleMetadata(withBlock(percentile, null))).toThrow();
    expect(() => parseBundleMetadata(withBlock(percentile, 75))).toThrow();
    expect(() => parseBundleMetadata(withBlock(percentile, {}))).toThrow();
    expect(() => parseBundleMetadata(withBlock(percentile, { percentileValue: 75, derivedForecast: 0 }))).toThrow();
    expect(() => parseBundleMetadata(withBlock(percentile, { percentileValue: 75, extra: 1 }))).toThrow();
    // Ranges.
    expect(() => parseBundleMetadata(withBlock(percentile, { percentileValue: 101 }))).toThrow();
    expect(() => parseBundleMetadata(withBlock(percentile, { percentileValue: -1 }))).toThrow();
    expect(() => parseBundleMetadata(withBlock(percentile, { percentileValue: 7.5 }))).toThrow();
    expect(() => parseBundleMetadata(withBlock(percentile, { derivedForecast: 256 }))).toThrow();
    const block = probability.ensemble as Extract<BundleEnsemble, { probabilityType: number }>;
    expect(() => parseBundleMetadata(withBlock(probability, { ...block, probabilityType: 6 }))).toThrow();
    expect(() => parseBundleMetadata(withBlock(probability, { ...block, scaleFactorOfLowerLimit: 128 }))).toThrow();
    expect(() => parseBundleMetadata(withBlock(probability, { ...block, scaledValueOfLowerLimit: 0xffffffff }))).toThrow();
    // A probability carries all five keys.
    const { scaledValueOfUpperLimit: _dropped, ...fourKeys } = block;
    expect(() => parseBundleMetadata(withBlock(probability, fourKeys))).toThrow();
    // A limit is both halves or neither.
    expect(() => parseBundleMetadata(withBlock(probability, { ...block, scaledValueOfLowerLimit: null }))).toThrow();
    // Type 3 (above the lower limit) uses the lower limit alone; type 1
    // (above the upper) the upper alone; type 2 (between) both.
    expect(() => parseBundleMetadata(withBlock(probability, { ...block, scaleFactorOfUpperLimit: 2, scaledValueOfUpperLimit: 1000 }))).toThrow();
    expect(() => parseBundleMetadata(withBlock(probability, { ...block, probabilityType: 1 }))).toThrow();
    expect(() => parseBundleMetadata(withBlock(probability, { ...block, probabilityType: 2 }))).toThrow();
    expect(
      parseBundleMetadata(
        withBlock(probability, { ...block, probabilityType: 2, scaleFactorOfUpperLimit: 2, scaledValueOfUpperLimit: 2500 }),
      ).variables[0]!.ensemble,
    ).toMatchObject({ probabilityType: 2 });
    // Never below schema version 3.
    const legacy = JSON.parse(metadata(percentile)) as { schemaVersion: number; variables: Record<string, unknown>[] };
    legacy.schemaVersion = 2;
    delete legacy.variables[0]!.parameter;
    expect(() => parseBundleMetadata(JSON.stringify(legacy))).toThrow();
  });

  it("reads a probability's axis from its first frame, the 24th hour", () => {
    const parsed = parseBundleMetadata(metadata(bundleVariable("apcp24gt10"), 24));
    expect(frameOffsets(parsed.time)).toEqual([24, 30, 36]);
  });
});

describe("the ensemble identities", () => {
  it("resolves every statistic's blocks to its own id, and back", () => {
    for (const id of ENSEMBLE_IDS) {
      const entry = registry[id]!;
      const identity = identityForParameter(entry.parameter, undefined, undefined, entry.ensemble);
      expect(identity, id).not.toBeNull();
      expect(identity!.statistic).toBe(statisticKey(entry.ensemble!));
      expect(registeredBundleId(identity)).toBe(id);
      expect(sameIdentity(identityForBundleId(id), identity)).toBe(true);
      // A file named anything at all reads as the statistic it carries.
      expect(registeredBundleId(identifyBundle([{ ...bundleVariable(id), id: "x" }])!.identity)).toBe(id);
    }
  });

  it("charts a percentile or a mean as its quantity, and tells it from the quantity", () => {
    for (const base of QUARTILE_BASES) {
      const quantity = identityForBundleId(base)!;
      const upper = identifyBundle([bundleVariable(`${base}q75`)])!.identity;
      expect(upper.family).toBe(quantity.family);
      expect(upper.level).toBe(quantity.level);
      expect(sameIdentity(upper, quantity)).toBe(false);
      expect(sameIdentity(upper, identityForBundleId(`${base}q25`))).toBe(false);
      expect(statisticBase(`${base}q75`)).toBe(base);
    }
    // The median published under the quantity's id is that quantity's chart.
    const median = identifyBundle([centreVariable("tmp2m", { percentileValue: 50 })])!.identity;
    expect(median.statistic).toBe("q50");
    expect(registeredBundleId(median)).toBe("tmp2m");
    expect(sameChart(median, identityForBundleId("tmp2m"))).toBe(true);
    expect(sameIdentity(median, identityForBundleId("tmp2m"))).toBe(false);
    const mean = identifyBundle([centreVariable("prmsl", { derivedForecast: 0 })])!.identity;
    expect(registeredBundleId(mean)).toBe("prmsl");
    // The same quartile draws on the same palette as its quantity.
    const quality = registry.tmp2mq75!.quality;
    const own = registryEntries<LinearQuantization>(["tmp2m"]).tmp2m!;
    expect(quality).toEqual(own.quality);
    expect(buildPalette(bundleVariable("tmp2mq75"), identifyBundle([bundleVariable("tmp2mq75")])!.identity)).toEqual(
      buildPalette(registryVariable("tmp2m", own, own.quality), identityForBundleId("tmp2m")),
    );
  });

  it("never charts a spread or a probability as the quantity", () => {
    const spread = identifyBundle([bundleVariable("prmslsd")])!.identity;
    expect(spread.family).toBe("hgtsd");
    expect(spread.level).toBeNull();
    expect(identifyBundle([bundleVariable("hgt500sd")])!.identity).toMatchObject({ family: "hgtsd", level: 500 });
    expect(sameChart(spread, identityForBundleId("prmsl"))).toBe(false);
    expect(statisticBase("prmslsd")).toBe("prmsl");
    expect(statisticBase("hgt500sd")).toBe("hgt500");
    // A spread of a quantity with no spread chart is unknown, not the quantity.
    const tmp2m = registryEntries<LinearQuantization>(["tmp2m"]).tmp2m!;
    expect(identityForParameter(tmp2m.parameter, undefined, undefined, { derivedForecast: 4 })).toBeNull();
    // A probability on a quantity no probability is registered for is unknown.
    expect(
      identityForParameter(tmp2m.parameter, undefined, undefined, registry.apcp24gt10!.ensemble),
    ).toBeNull();
    const probability = identifyBundle([bundleVariable("apcp24gt10")])!.identity;
    expect(probability.family).toBe("pop");
    expect(statisticBase("apcp24gt10")).toBeNull();
    // The thresholds are different fields.
    expect(sameIdentity(probability, identifyBundle([bundleVariable("apcp24gt25")])!.identity)).toBe(false);
  });

  it("paints the probability over 0–100 % and the spread clear where members agree", () => {
    const probability = bundleVariable("apcp24gt10");
    const palette = buildPalette(probability, identifyBundle([probability])!.identity);
    expect(rgba(palette, 0)[3]).toBe(0);
    expect(rgba(palette, 200)[3]).toBeGreaterThan(200);
    expect(scalarLegendRange(identityForBundleId("apcp24gt10")!)).toEqual([0, 100]);
    const spread = bundleVariable("prmslsd");
    expect(rgba(buildPalette(spread, identifyBundle([spread])!.identity), 0)[3]).toBe(0);
    expect(variableSpec("prmslsd")!.legend()).toEqual(["10", "8", "6", "4", "2", "0"]);
  });
});

describe("the statistic group", () => {
  it("offers the quartiles around the median, and the mean beside the spread", () => {
    const codes = (id: string) => statisticMembers(id, cmageps).map((member) => [member.id, member.code]);
    expect(codes("tmp2m")).toEqual([
      ["tmp2mq25", "25%"],
      ["tmp2m", "MEDIAN"],
      ["tmp2mq75", "75%"],
    ]);
    // From any member, the same group.
    expect(codes("tmp2mq75")).toEqual(codes("tmp2m"));
    expect(codes("prateq25")).toEqual([
      ["prateq25", "25%"],
      ["prate", "MEDIAN"],
      ["prateq75", "75%"],
    ]);
    expect(codes("prmsl")).toEqual([
      ["prmsl", "MEAN"],
      ["prmslsd", "SPREAD"],
    ]);
    expect(codes("hgt500sd")).toEqual([
      ["hgt500", "MEAN"],
      ["hgt500sd", "SPREAD"],
    ]);
  });

  it("has nothing to offer where no statistic is published", () => {
    expect(statisticMembers("tmp2m", (id) => id === "tmp2m")).toEqual([]);
    expect(statisticMembers("dpt2m", cmageps)).toEqual([]);
    expect(statisticMembers("apcp24gt10", cmageps)).toEqual([]);
    // A statistic whose quantity the run does not publish has no centre.
    expect(statisticMembers("tmp2mq75", (id) => id === "tmp2mq75")).toEqual([]);
  });

  it("notes the statistic a reading is", () => {
    expect(statisticNote(undefined, "")).toBeNull();
    expect(statisticNote("q50", "")).toBe("median");
    expect(statisticNote("q75", "")).toBe("75th pct");
    expect(statisticNote("mean", "")).toBe("mean");
    expect(statisticNote("spread", "")).toBe("spread");
    expect(statisticNote("p3:10:", "mm")).toBe("P>10 mm");
  });
});

describe("the probability family", () => {
  it("lists the six thresholds as its level row, lowest first", () => {
    const ids = THRESHOLDS.map((threshold) => `apcp24gt${threshold}`);
    expect(familyLevels("pop", cmageps)).toEqual(ids);
    expect(FAMILIES.pop.surface).toBe("apcp24gt1");
    expect(FAMILIES.pop.captionKey).toBe("thresholdCaption");
    for (const [index, id] of ids.entries()) {
      expect(familyOf(id)).toBe("pop");
      expect(levelCode(id)).toBe(`>${THRESHOLDS[index]}`);
      expect(variableSpec(id)!.family).toBe("pop");
      expect(variableSpec(id)!.group).toBe("moisture");
    }
    expect(FORECAST_MODELS.cmageps.railCore).toContain("apcp24gt1");
  });

  it("names its window and threshold", () => {
    expect(probabilityLabel("apcp24gt10")).toBe("Probability of 24-hour precipitation above 10 mm");
    expect(variableSpec("apcp24gt10")!.label()).toBe(probabilityLabel("apcp24gt10"));
    expect(variableSpec("apcp24gt100")!.code).toBe("APCP 24H >100");
  });
});

describe("the meteogram's quartile band", () => {
  it("reads a row's quartiles where the run publishes both", () => {
    const rows = meteogramRows(cmageps);
    const temperature = rows.find((row) => row.id === "temperature")!;
    expect(temperature.quartiles).toEqual(["tmp2mq25", "tmp2mq75"]);
    expect(meteogramRowBundles(temperature)).toEqual(["tmp2m", "tmp2mq25", "tmp2mq75"]);
    expect(rows.find((row) => row.id === "precipitation")!.quartiles).toEqual(["prateq25", "prateq75"]);
    expect(rows.find((row) => row.id === "wind")!.quartiles).toEqual(["gustq25", "gustq75"]);
    // The cloud row is shaded bands already: no quartile band over them.
    expect(rows.find((row) => row.id === "cloud")!.quartiles).toBeUndefined();
  });

  it("draws none for a single forecast, or for one quartile alone", () => {
    for (const row of meteogramRows((id) => ["tmp2m", "prate", "gust"].includes(id))) expect(row.quartiles).toBeUndefined();
    const temperature = meteogramRows((id) => id === "tmp2m" || id === "tmp2mq75").find((row) => row.id === "temperature")!;
    expect(temperature.quartiles).toBeUndefined();
  });
});
