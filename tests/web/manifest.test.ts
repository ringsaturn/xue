import { describe, expect, it } from "vitest";

import { CRC32_INITIAL, crc32Hex, crc32Of, crc32Update } from "../../web/src/crc32";
import { hasBundle, hasWindBundle, parseBundleMetadata, pickBundleVariant, validateManifest } from "../../web/src/manifest";
import { buildPalette, buildWindSpeedPalette, decodeLinear, decodeLog } from "../../web/src/palettes";
import type { BundleVariable, LogQuantization, VariantDescriptor } from "../../web/src/manifest";

function videoMetadataFixture() {
  return JSON.stringify({
    schemaVersion: 1,
    model: "GFS",
    product: "pgrb2.0p25",
    runTime: "2026-08-15T06:00:00Z",
    time: { firstForecastHour: 0, stepHours: 1, frameCount: 121 },
    grid: { width: 1440, height: 721 },
    variables: [
      {
        numericId: 1,
        id: "tmp2m",
        label: "2 meter temperature",
        unit: "°C",
        quantization: {
          type: "linear",
          offset: -60,
          scale: 0.5,
          minimumCode: 0,
          maximumCode: 220,
          nodataCode: 255,
        },
      },
    ],
  });
}

function videoFixture() {
  return {
    streamPath: "gfs.2026081506/tmp2m.h264",
    indexPath: "gfs.2026081506/tmp2m.h264.index.json",
    byteLength: 19_867_506,
    crc32: "c63a61aa",
    codec: "avc1.f40028",
    width: 1440,
    height: 721,
    gop: 6,
    frameCount: 121,
    metadataJson: videoMetadataFixture(),
  };
}

function variantFixture(): VariantDescriptor {
  return {
    path: "gfs.2026081506/tmp2m.half.xue",
    width: 720,
    height: 361,
    byteLength: 11_000_000,
    crc32: "12345678",
    bandwidth: 10_500_000,
  };
}

function manifestFixture() {
  return {
    schemaVersion: 5,
    model: "GFS",
    product: "pgrb2.0p25",
    runTime: "2026-08-15T06:00:00Z",
    forecastHours: 120,
    bundles: [
      {
        variable: "tmp2m",
        path: "gfs.2026081506/tmp2m.xue",
        byteLength: 45_631_552,
        crc32: "35814edd",
        variants: [variantFixture()],
        video: videoFixture(),
      },
      {
        variable: "prate",
        path: "gfs.2026081506/prate.xue",
        byteLength: 25_822_504,
        crc32: "1234abcd",
      },
    ],
  };
}

describe("validateManifest", () => {
  it("accepts a valid schema v5 manifest with a video descriptor", () => {
    const manifest = validateManifest(manifestFixture());
    expect(manifest.bundles.map((bundle) => bundle.variable)).toEqual(["tmp2m", "prate"]);
    expect(manifest.bundles[0]!.path).toBe("gfs.2026081506/tmp2m.xue");
    expect(manifest.bundles[0]!.video?.codec).toBe("avc1.f40028");
    expect(manifest.bundles[1]!.video).toBeUndefined();
  });

  it("rejects an invalid video descriptor shape", () => {
    const missingMetadata = manifestFixture();
    const { metadataJson: _metadataJson, ...rest } = missingMetadata.bundles[0]!.video!;
    missingMetadata.bundles[0]!.video = rest as ReturnType<typeof videoFixture>;
    expect(() => validateManifest(missingMetadata)).toThrow("metadata");

    const badStreamPath = manifestFixture();
    badStreamPath.bundles[0]!.video!.streamPath = "gfs.2026081506/tmp2m.mp4";
    expect(() => validateManifest(badStreamPath)).toThrow("path");
  });

  it("rejects other schema versions", () => {
    expect(() => validateManifest({ ...manifestFixture(), schemaVersion: 3 })).toThrow("manifest schema version");
  });

  it("accepts an ECMWF manifest and enforces the model/product pairing", () => {
    const ecmwf = { ...manifestFixture(), model: "ECMWF", product: "ifs-0p25" };
    expect(validateManifest(ecmwf).model).toBe("ECMWF");
    expect(validateManifest(ecmwf, "ecmwf").model).toBe("ECMWF");
    expect(() => validateManifest(ecmwf, "gfs")).toThrow("model");
    expect(() => validateManifest({ ...manifestFixture(), model: "ECMWF" })).toThrow("product");
    expect(() => validateManifest({ ...manifestFixture(), model: "ICON" })).toThrow("product");
  });

  it("rejects invalid bundle paths", () => {
    for (const path of ["/abs.xue", "http://x/y.xue", "a/../b.xue", "bundle.pmtiles", "gfs.2026081506/tmp2m.bin"]) {
      const broken = manifestFixture();
      broken.bundles[0]!.path = path;
      expect(() => validateManifest(broken)).toThrow("path");
    }
  });

  it("rejects duplicate bundle paths", () => {
    const broken = manifestFixture();
    broken.bundles[1]!.path = broken.bundles[0]!.path;
    expect(() => validateManifest(broken)).toThrow("duplicate");
  });

  it("rejects a manifest missing a variable's bundle", () => {
    const broken = manifestFixture();
    broken.bundles = broken.bundles.slice(0, 1);
    expect(() => validateManifest(broken)).toThrow("prate");
    const unknown = manifestFixture();
    unknown.bundles[0]!.variable = "gust10m";
    expect(() => validateManifest(unknown)).toThrow("variable");
  });

  it("accepts the optional wind10m bundle and reports it via hasWindBundle", () => {
    const withoutWind = validateManifest(manifestFixture());
    expect(hasWindBundle(withoutWind)).toBe(false);

    const withWind = manifestFixture();
    withWind.bundles.push({
      variable: "wind10m",
      path: "gfs.2026081506/wind10m.xue",
      byteLength: 52_000_000,
      crc32: "0badf00d",
    });
    const manifest = validateManifest(withWind);
    expect(manifest.bundles.map((bundle) => bundle.variable)).toEqual(["tmp2m", "prate", "wind10m"]);
    expect(hasWindBundle(manifest)).toBe(true);
  });

  it("accepts a GFS-SFLUX manifest with the optional dswrf bundle", () => {
    const sflux = manifestFixture();
    sflux.model = "GFS-SFLUX";
    sflux.product = "sfluxgrb";
    sflux.bundles[0]!.path = "sflux.2026081506/tmp2m.xue";
    sflux.bundles[0]!.variants![0]!.path = "sflux.2026081506/tmp2m.half.xue";
    sflux.bundles[0]!.video!.streamPath = "sflux.2026081506/tmp2m.h264";
    sflux.bundles[0]!.video!.indexPath = "sflux.2026081506/tmp2m.h264.index.json";
    sflux.bundles[1]!.path = "sflux.2026081506/prate.xue";
    sflux.bundles.push({
      variable: "dswrf",
      path: "sflux.2026081506/dswrf.xue",
      byteLength: 61_000_000,
      crc32: "5011a860",
    });
    const manifest = validateManifest(sflux, "sflux");
    expect(manifest.bundles.map((bundle) => bundle.variable)).toEqual(["tmp2m", "prate", "dswrf"]);
    expect(hasBundle(manifest, "dswrf")).toBe(true);
    expect(hasBundle(manifest, "wind10m")).toBe(false);
    expect(hasBundle(validateManifest(manifestFixture()), "dswrf")).toBe(false);
    expect(() => validateManifest(sflux, "gfs")).toThrow("model");
    expect(() => validateManifest({ ...sflux, product: "pgrb2.0p25" })).toThrow("product");
  });

  it("rejects invalid byteLength and crc32", () => {
    const zero = manifestFixture();
    zero.bundles[0]!.byteLength = 0;
    expect(() => validateManifest(zero)).toThrow("byteLength");
    const badCrc = manifestFixture();
    badCrc.bundles[0]!.crc32 = "XYZ";
    expect(() => validateManifest(badCrc)).toThrow("crc32");
  });

  it("rejects a wrong forecast range", () => {
    expect(() => validateManifest({ ...manifestFixture(), forecastHours: 24 })).toThrow("120");
  });

  it("accepts variants and rejects broken variant descriptors", () => {
    const manifest = validateManifest(manifestFixture());
    expect(manifest.bundles[0]!.variants?.[0]?.width).toBe(720);
    expect(manifest.bundles[1]!.variants).toBeUndefined();

    const badPath = manifestFixture();
    badPath.bundles[0]!.variants![0]!.path = "gfs.2026081506/tmp2m.half.mp4";
    expect(() => validateManifest(badPath)).toThrow("path");

    const duplicate = manifestFixture();
    duplicate.bundles[0]!.variants![0]!.path = duplicate.bundles[0]!.path;
    expect(() => validateManifest(duplicate)).toThrow("duplicate");

    const zeroBandwidth = manifestFixture();
    zeroBandwidth.bundles[0]!.variants![0]!.bandwidth = 0;
    expect(() => validateManifest(zeroBandwidth)).toThrow("bandwidth");

    const empty = manifestFixture();
    empty.bundles[0]!.variants = [];
    expect(() => validateManifest(empty)).toThrow("variant");
  });
});

describe("pickBundleVariant", () => {
  const half = variantFixture();

  it("returns null without variants", () => {
    expect(pickBundleVariant(undefined, 500, false)).toBeNull();
    expect(pickBundleVariant([], 500, true)).toBeNull();
  });

  it("always picks the smallest tier on constrained networks", () => {
    expect(pickBundleVariant([half], 4000, true)).toEqual(half);
  });

  it("picks the smallest tier that covers the view, else full resolution", () => {
    // Zoomed far out on a 1x display: 720 columns cover the view.
    expect(pickBundleVariant([half], 512, false)).toEqual(half);
    // Default view on a retina display needs more than 720 columns.
    expect(pickBundleVariant([half], 1607, false)).toBeNull();
  });
});

describe("crc32", () => {
  it("matches the IEEE reference vector", () => {
    // CRC-32/IEEE("123456789") = 0xCBF43926.
    expect(crc32Of(new TextEncoder().encode("123456789"))).toBe("cbf43926");
  });

  it("is chunking-invariant", () => {
    const data = new Uint8Array(1024).map((_, index) => (index * 31) & 0xff);
    let crc = CRC32_INITIAL;
    for (let offset = 0; offset < data.length; offset += 100) {
      crc = crc32Update(crc, data.subarray(offset, offset + 100));
    }
    expect(crc32Hex(crc)).toBe(crc32Of(data));
  });
});

describe("parseBundleMetadata", () => {
  const metadata = {
    schemaVersion: 1,
    model: "GFS",
    product: "pgrb2.0p25",
    runTime: "2026-08-15T06:00:00Z",
    time: { firstForecastHour: 0, stepHours: 1, frameCount: 121 },
    grid: { width: 1440, height: 721 },
    variables: [
      {
        numericId: 1,
        id: "tmp2m",
        label: "2 meter temperature",
        unit: "°C",
        quantization: {
          type: "linear",
          offset: -60,
          scale: 0.5,
          minimumCode: 0,
          maximumCode: 220,
          nodataCode: 255,
        },
      },
    ],
  };

  it("accepts valid embedded metadata", () => {
    const parsed = parseBundleMetadata(JSON.stringify(metadata));
    expect(parsed.time.frameCount).toBe(121);
  });

  it("rejects unsupported schema versions and broken time axes", () => {
    expect(() => parseBundleMetadata(JSON.stringify({ ...metadata, schemaVersion: 2 }))).toThrow();
    expect(() =>
      parseBundleMetadata(JSON.stringify({ ...metadata, time: { frameCount: 0 } })),
    ).toThrow();
  });
});

describe("palettes", () => {
  const logQuantization: LogQuantization = {
    type: "log1p",
    trace: 0.01,
    scale: 0.05,
    maximum: 128,
    minimumCode: 1,
    maximumCode: 253,
    zeroCode: 0,
    overflowCode: 254,
    nodataCode: 255,
  };

  it("decodes linear codes and rejects invalid ones", () => {
    const quantization = {
      type: "linear",
      offset: -60,
      scale: 0.5,
      minimumCode: 0,
      maximumCode: 220,
      nodataCode: 255,
    } as const;
    expect(decodeLinear(quantization, 0)).toBe(-60);
    expect(decodeLinear(quantization, 220)).toBe(50);
    expect(decodeLinear(quantization, 255)).toBeNull();
  });

  it("decodes log codes with a strictly increasing overflow step", () => {
    expect(decodeLog(logQuantization, 0)).toBe(0);
    expect(decodeLog(logQuantization, 253)).toBeCloseTo(128, 6);
    const overflow = decodeLog(logQuantization, 254);
    expect(overflow).not.toBeNull();
    expect(overflow!).toBeGreaterThan(128);
    expect(decodeLog(logQuantization, 255)).toBeNull();
  });

  it("builds a 256x1 RGBA palette with transparent dry and nodata codes", () => {
    const variable: BundleVariable = {
      numericId: 2,
      id: "prate",
      label: "Precipitation rate",
      unit: "mm/h",
      quantization: logQuantization,
    };
    const palette = buildPalette(variable);
    expect(palette.length).toBe(1024);
    expect(palette[3]).toBe(0); // code 0: dry, fully transparent
    expect(palette[253 * 4 + 3]).toBe(255); // top in-range code opaque
    expect(palette[255 * 4 + 3]).toBe(0); // nodata transparent
  });

  it("builds the solar ramp with a transparent night side and opaque noon glare", () => {
    const variable: BundleVariable = {
      numericId: 5,
      id: "dswrf",
      label: "Downward shortwave radiation flux",
      unit: "W/m²",
      quantization: {
        type: "linear",
        offset: 0,
        scale: 5,
        minimumCode: 0,
        maximumCode: 254,
        nodataCode: 255,
      },
    };
    const palette = buildPalette(variable);
    expect(palette.length).toBe(1024);
    expect(palette[3]).toBe(0); // 0 W/m²: night, fully transparent
    expect(palette[254 * 4 + 3]).toBe(255); // 1270 W/m² opaque
    expect(palette[255 * 4 + 3]).toBe(0); // nodata transparent
    // High end is warm (red-dominant), unlike the temperature ramp's violet top.
    expect(palette[254 * 4]!).toBeGreaterThan(200);
  });

  it("builds an opaque wind speed ramp from calm blue to violent violet", () => {
    const palette = buildWindSpeedPalette();
    expect(palette.length).toBe(1024);
    expect(palette[3]).toBe(255); // calm end opaque
    expect(palette[255 * 4 + 3]).toBe(255); // ramp ceiling opaque
    expect(palette[2]!).toBeGreaterThan(palette[0]!); // blue-dominant at 0 m/s
    expect(palette[255 * 4]!).toBeGreaterThan(palette[255 * 4 + 1]!); // red over green at 40 m/s
  });
});
