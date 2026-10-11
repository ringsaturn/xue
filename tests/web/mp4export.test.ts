import { afterEach, describe, expect, it, vi } from "vitest";

import {
  isMp4ExportSupported,
  MP4_ACCELERATION,
  MP4_CODECS,
  MP4_FPS,
  mp4Bitrate,
  mp4CodecCandidates,
  mp4EncoderConfig,
  mp4FileName,
  mp4Schedule,
  mp4Size,
} from "../../web/src/mp4export";

describe("mp4Bitrate", () => {
  it("is 9 Mbit/s at 1080p30", () => {
    expect(mp4Bitrate(1920, 1080, 30)).toBe(9_000_000);
  });

  it("scales a little under linearly with the pixel count", () => {
    const quarter = mp4Bitrate(960, 540, 30);
    expect(quarter).toBeGreaterThan(9e6 / 4);
    expect(quarter).toBeLessThan(9e6 / 2);
    expect(quarter).toBe(Math.round(9e6 * 0.25 ** 0.9));
  });

  it("scales with the frame rate", () => {
    expect(mp4Bitrate(1920, 1080, 12)).toBe(Math.round(9e6 * (12 / 30) ** 0.6));
  });

  it("is clamped at both ends", () => {
    expect(mp4Bitrate(64, 64, 30)).toBe(1_500_000);
    expect(mp4Bitrate(7680, 4320, 60)).toBe(45_000_000);
  });
});

describe("mp4Size", () => {
  it("scales a wide canvas down to the cap", () => {
    expect(mp4Size(2880, 1800)).toEqual({ width: 1920, height: 1200 });
  });

  it("never scales up and keeps both sides even", () => {
    expect(mp4Size(391, 845)).toEqual({ width: 392, height: 846 });
    expect(mp4Size(1280, 720)).toEqual({ width: 1280, height: 720 });
  });
});

describe("mp4Schedule", () => {
  it("samples each data frame for as long as playback holds it", () => {
    // 12 fps playback: 83.3 ms a frame, 2.5 ticks of a 30 fps clock.
    const holds = [1000 / 12, 1000 / 12, 1000 / 12, 1000 / 12];
    const steps = mp4Schedule(holds, 30);
    expect(steps).toHaveLength(10);
    expect(steps.map((step) => step.frame)).toEqual([0, 0, 0, 1, 1, 2, 2, 2, 3, 3]);
  });

  it("sweeps the weight through each frame's span and holds the last", () => {
    const steps = mp4Schedule([100, 100, 100], 20);
    expect(steps.map((step) => step.frame)).toEqual([0, 0, 1, 1, 2, 2]);
    expect(steps.map((step) => step.weight)).toEqual([0, 0.5, 0, 0.5, 0, 0]);
  });

  it("gives a longer step proportionally more pictures", () => {
    // The 3-hourly tail holds three times as long as the hourly frames.
    const steps = mp4Schedule([100, 100, 300, 300], 10);
    const perFrame = [0, 1, 2, 3].map((frame) => steps.filter((step) => step.frame === frame).length);
    expect(perFrame).toEqual([1, 1, 3, 3]);
  });

  it("is empty for no frames and one picture for a single frame", () => {
    expect(mp4Schedule([], 30)).toEqual([]);
    expect(mp4Schedule([10], 30)).toEqual([{ frame: 0, weight: 0 }]);
  });

  it("keeps every weight below one", () => {
    for (const step of mp4Schedule([83, 91, 77, 250, 83], MP4_FPS)) {
      expect(step.weight).toBeGreaterThanOrEqual(0);
      expect(step.weight).toBeLessThan(1);
    }
  });
});

describe("mp4FileName", () => {
  it("mirrors the GIF's name with the video's extension", () => {
    expect(mp4FileName("GFS / PRATE SFC", Date.UTC(2026, 9, 5, 12, 0))).toBe("xue-gfs-prate-sfc-20261005t1200z.mp4");
    expect(mp4FileName("", Date.UTC(2026, 0, 1))).toBe("xue-map-20260101t0000z.mp4");
  });
});

describe("mp4CodecCandidates", () => {
  it("tries High, Main then Baseline, hardware before no preference", () => {
    const candidates = mp4CodecCandidates(1920, 1080, 30);
    expect(candidates.map((candidate) => [candidate.codec, candidate.hardwareAcceleration])).toEqual([
      ["avc1.640028", "prefer-hardware"],
      ["avc1.640028", "no-preference"],
      ["avc1.4d0028", "prefer-hardware"],
      ["avc1.4d0028", "no-preference"],
      ["avc1.42e028", "prefer-hardware"],
      ["avc1.42e028", "no-preference"],
    ]);
    expect(MP4_CODECS).toHaveLength(3);
    expect(MP4_ACCELERATION).toEqual(["prefer-hardware", "no-preference"]);
  });

  it("asks for quality, variable bitrate and avc-format chunks", () => {
    const [first] = mp4CodecCandidates(1280, 720, 30);
    expect(first).toMatchObject({
      width: 1280,
      height: 720,
      framerate: 30,
      bitrate: mp4Bitrate(1280, 720, 30),
      latencyMode: "quality",
      bitrateMode: "variable",
      avc: { format: "avc" },
    });
  });
});

describe("support detection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("is unsupported without VideoEncoder", async () => {
    vi.stubGlobal("VideoEncoder", undefined);
    vi.stubGlobal("VideoFrame", undefined);
    expect(await mp4EncoderConfig(1280, 720)).toBeNull();
  });

  it("takes the first configuration the encoder accepts", async () => {
    const asked: string[] = [];
    vi.stubGlobal("VideoFrame", class {});
    vi.stubGlobal("VideoEncoder", {
      isConfigSupported: async (config: VideoEncoderConfig) => {
        asked.push(`${config.codec}/${config.hardwareAcceleration}`);
        const supported = config.codec === "avc1.4d0028" && config.hardwareAcceleration === "no-preference";
        return { supported, config: supported ? config : undefined };
      },
    });
    const config = await mp4EncoderConfig(1280, 720);
    expect(config?.codec).toBe("avc1.4d0028");
    expect(asked).toEqual([
      "avc1.640028/prefer-hardware",
      "avc1.640028/no-preference",
      "avc1.4d0028/prefer-hardware",
      "avc1.4d0028/no-preference",
    ]);
  });

  it("treats a rejected candidate as unsupported and goes on", async () => {
    vi.stubGlobal("VideoFrame", class {});
    let calls = 0;
    vi.stubGlobal("VideoEncoder", {
      isConfigSupported: async () => {
        calls += 1;
        if (calls < 6) throw new TypeError("unknown option");
        return { supported: true };
      },
    });
    const config = await mp4EncoderConfig(1280, 720);
    expect(config?.codec).toBe("avc1.42e028");
    expect(config?.hardwareAcceleration).toBe("no-preference");
  });

  it("reports no support when every candidate is refused", async () => {
    vi.stubGlobal("VideoFrame", class {});
    vi.stubGlobal("VideoEncoder", { isConfigSupported: async () => ({ supported: false }) });
    expect(await mp4EncoderConfig(1280, 720)).toBeNull();
    // The probe is cached for the page's life, so it is read once here.
    expect(await isMp4ExportSupported()).toBe(false);
  });
});
