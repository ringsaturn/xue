import { deflateSync } from "node:zlib";
import { describe, expect, it } from "vitest";

import { validateLatestPointer } from "../../web/src/manifest";
import { decodePosterPlane, isPosterSupported } from "../../web/src/poster";

function encodePoster(plane: Uint8Array, width: number, height: number): Uint8Array {
  // Mirror of xue.binconvert.encode_poster's filter stage: each row
  // delta-encoded against the previous one (uint8 wraparound), then deflate.
  const filtered = new Uint8Array(plane);
  for (let row = height - 1; row >= 1; row -= 1) {
    for (let column = 0; column < width; column += 1) {
      filtered[row * width + column] =
        (plane[row * width + column]! - plane[(row - 1) * width + column]! + 256) & 0xff;
    }
  }
  return new Uint8Array(deflateSync(filtered));
}

describe("decodePosterPlane", () => {
  it.runIf(isPosterSupported())("roundtrips a filtered deflated plane", async () => {
    const width = 72;
    const height = 37;
    const plane = new Uint8Array(width * height).map((_, index) => (index * 37 + (index % 13) * 5) & 0xff);
    const payload = encodePoster(plane, width, height);
    const decoded = await decodePosterPlane(payload, width, height);
    expect(decoded).toEqual(plane);
  });

  it.runIf(isPosterSupported())("rejects a plane whose size disagrees", async () => {
    const payload = encodePoster(new Uint8Array(10), 5, 2);
    await expect(decodePosterPlane(payload, 5, 3)).rejects.toThrow("length");
  });
});

describe("validateLatestPointer", () => {
  const pointer = {
    schemaVersion: 1,
    model: "GFS",
    product: "pgrb2.0p25",
    run: "2026081600",
    runTime: "2026-08-16T00:00:00Z",
    manifestPath: "gfs.2026081600/manifest.json",
    manifestCrc32: "0123abcd",
  };

  it("accepts a valid pointer", () => {
    expect(validateLatestPointer(pointer).run).toBe("2026081600");
  });

  it("rejects invalid runs, paths, and checksums", () => {
    expect(() => validateLatestPointer({ ...pointer, run: "latest" })).toThrow("run id");
    expect(() => validateLatestPointer({ ...pointer, manifestPath: "/abs/manifest.json" })).toThrow("path");
    expect(() => validateLatestPointer({ ...pointer, manifestPath: "https://x/manifest.json" })).toThrow("path");
    expect(() => validateLatestPointer({ ...pointer, manifestCrc32: "nope" })).toThrow("crc32");
    expect(() => validateLatestPointer({ ...pointer, schemaVersion: 2 })).toThrow("version");
  });
});
