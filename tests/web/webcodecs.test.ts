import { describe, expect, it } from "vitest";

import { gopByteSpan, gopRange, isWebCodecsSupported, nearestKeyframe, type VideoFrameIndexEntry } from "../../web/src/webcodecs";

describe("nearestKeyframe", () => {
  const frames: VideoFrameIndexEntry[] = [
    { offset: 0, length: 10, keyframe: true },
    { offset: 10, length: 5, keyframe: false },
    { offset: 15, length: 5, keyframe: false },
    { offset: 20, length: 10, keyframe: true },
    { offset: 30, length: 5, keyframe: false },
  ];

  it("returns the target itself when it is a keyframe", () => {
    expect(nearestKeyframe(frames, 0)).toBe(0);
    expect(nearestKeyframe(frames, 3)).toBe(3);
  });

  it("walks backward to the nearest preceding keyframe", () => {
    expect(nearestKeyframe(frames, 2)).toBe(0);
    expect(nearestKeyframe(frames, 4)).toBe(3);
  });

  it("falls back to frame 0 when nothing before it is flagged as a keyframe", () => {
    const noLeadingKeyframe: VideoFrameIndexEntry[] = [
      { offset: 0, length: 10, keyframe: false },
      { offset: 10, length: 10, keyframe: false },
    ];
    expect(nearestKeyframe(noLeadingKeyframe, 1)).toBe(0);
  });
});

describe("gopRange", () => {
  const frames: VideoFrameIndexEntry[] = [
    { offset: 0, length: 10, keyframe: true },
    { offset: 10, length: 5, keyframe: false },
    { offset: 15, length: 5, keyframe: false },
    { offset: 20, length: 10, keyframe: true },
    { offset: 30, length: 5, keyframe: false },
  ];

  it("spans the full GOP for a mid-GOP target", () => {
    expect(gopRange(frames, 1)).toEqual({ from: 0, to: 2 });
    expect(gopRange(frames, 2)).toEqual({ from: 0, to: 2 });
  });

  it("spans the full GOP when the target is its keyframe", () => {
    expect(gopRange(frames, 0)).toEqual({ from: 0, to: 2 });
    expect(gopRange(frames, 3)).toEqual({ from: 3, to: 4 });
  });

  it("stops at the end of the stream", () => {
    expect(gopRange(frames, 4)).toEqual({ from: 3, to: 4 });
  });
});

describe("gopByteSpan", () => {
  const frames: VideoFrameIndexEntry[] = [
    { offset: 0, length: 10, keyframe: true },
    { offset: 10, length: 5, keyframe: false },
    { offset: 15, length: 5, keyframe: false },
    { offset: 20, length: 10, keyframe: true },
    { offset: 30, length: 5, keyframe: false },
  ];

  it("covers the first byte of the keyframe through the last byte of the GOP tail", () => {
    expect(gopByteSpan(frames, 0, 2)).toEqual({ start: 0, end: 20 });
    expect(gopByteSpan(frames, 3, 4)).toEqual({ start: 20, end: 35 });
  });

  it("covers a single frame when from equals to", () => {
    expect(gopByteSpan(frames, 1, 1)).toEqual({ start: 10, end: 15 });
  });

  it("rejects out-of-index ranges", () => {
    expect(() => gopByteSpan(frames, 3, 5)).toThrow();
  });
});

describe("isWebCodecsSupported", () => {
  it("returns false when the browser has no VideoDecoder global (jsdom, Safari without the profile, etc.)", async () => {
    expect("VideoDecoder" in globalThis).toBe(false);
    await expect(isWebCodecsSupported("avc1.f40028", 1440, 721)).resolves.toBe(false);
  });
});
