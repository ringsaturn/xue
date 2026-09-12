import { afterEach, describe, expect, it } from "vitest";

import {
  browserZone,
  displayZone,
  formatCompactStamp,
  formatDayMark,
  formatStamp,
  isKnownZone,
  onDisplayZoneChange,
  setDisplayZone,
  useZoneFinder,
  zoneAt,
  zoneDisplayName,
  zoneOffsetLabel,
  zoneOffsetMinutes,
} from "../../web/src/timezone";

// 2026-09-12 15:00Z: a Saturday, and in Tokyo already Sunday.
const AT = Date.UTC(2026, 8, 12, 15, 0);

describe("zone offsets", () => {
  it("reads the offset a zone keeps at an instant, half hours included", () => {
    expect(zoneOffsetMinutes("UTC", AT)).toBe(0);
    expect(zoneOffsetMinutes("Asia/Tokyo", AT)).toBe(540);
    expect(zoneOffsetMinutes("Asia/Kolkata", AT)).toBe(330);
    expect(zoneOffsetMinutes("America/St_Johns", AT)).toBe(-150);
    // POSIX-inverted: Etc/GMT+10 is ten hours behind.
    expect(zoneOffsetMinutes("Etc/GMT+10", AT)).toBe(-600);
  });

  it("follows daylight saving through the year", () => {
    expect(zoneOffsetMinutes("America/New_York", AT)).toBe(-240);
    expect(zoneOffsetMinutes("America/New_York", Date.UTC(2026, 0, 12, 15, 0))).toBe(-300);
  });

  it("labels the offset as instrument text", () => {
    expect(zoneOffsetLabel("UTC", AT)).toBe("UTC");
    expect(zoneOffsetLabel("Asia/Tokyo", AT)).toBe("UTC+9");
    expect(zoneOffsetLabel("Asia/Kolkata", AT)).toBe("UTC+5:30");
    expect(zoneOffsetLabel("America/St_Johns", AT)).toBe("UTC-2:30");
    expect(zoneOffsetLabel("Etc/GMT+10", AT)).toBe("UTC-10");
  });

  it("names a zone by id, and the nameless ocean zones by offset", () => {
    expect(zoneDisplayName("Asia/Tokyo", AT)).toBe("Asia/Tokyo");
    expect(zoneDisplayName("America/New_York", AT)).toBe("America/New_York");
    expect(zoneDisplayName("Etc/GMT-12", AT)).toBe("UTC+12");
    expect(zoneDisplayName("Etc/UTC", AT)).toBe("UTC");
    expect(zoneDisplayName("UTC", AT)).toBe("UTC");
  });

  it("knows which ids the engine can format in", () => {
    expect(isKnownZone("Asia/Tokyo")).toBe(true);
    expect(isKnownZone("Etc/GMT-9")).toBe(true);
    expect(isKnownZone("Mars/Olympus_Mons")).toBe(false);
    expect(isKnownZone("")).toBe(false);
  });
});

describe("stamps", () => {
  it("writes the full stamp in the zone with its offset", () => {
    expect(formatStamp(AT, "UTC")).toBe("09/12 15:00 UTC");
    expect(formatStamp(AT, "Asia/Tokyo")).toBe("09/13 00:00 UTC+9");
    expect(formatStamp(AT, "America/New_York")).toBe("09/12 11:00 UTC-4");
    expect(formatStamp("2026-09-12T15:00:00Z", "Asia/Kolkata")).toBe("09/12 20:30 UTC+5:30");
  });

  it("keeps midnight at 00, never 24", () => {
    expect(formatStamp(Date.UTC(2026, 8, 12, 0, 0), "UTC")).toBe("09/12 00:00 UTC");
    expect(formatCompactStamp(Date.UTC(2026, 8, 12, 15, 0), "Asia/Tokyo")).toBe("09/13 00:00");
  });

  it("writes the compact stamp without the offset but with the minutes", () => {
    expect(formatCompactStamp(AT, "UTC")).toBe("09/12 15:00");
    expect(formatCompactStamp(AT, "Asia/Kolkata")).toBe("09/12 20:30");
  });

  it("marks a day with the zone's weekday and day of month", () => {
    expect(formatDayMark(AT, "UTC", "en")).toBe("Sat 12");
    expect(formatDayMark(AT, "Asia/Tokyo", "en")).toBe("Sun 13");
    expect(formatDayMark(AT, "Asia/Tokyo", "ja")).toBe("日 13");
    expect(formatDayMark(Date.UTC(2026, 8, 1, 15, 0), "America/New_York", "en")).toBe("Tue 01");
  });
});

describe("display zone", () => {
  // Whatever zone this machine keeps, a switch has to be to another one.
  const other = browserZone() === "America/New_York" ? "Asia/Tokyo" : "America/New_York";

  afterEach(() => {
    setDisplayZone(null);
    useZoneFinder(null);
  });

  it("starts on the browser's own zone", () => {
    expect(displayZone).toBe(browserZone());
    expect(isKnownZone(displayZone)).toBe(true);
  });

  it("switches in place and tells its listeners, once per change", async () => {
    let changes = 0;
    onDisplayZoneChange(() => (changes += 1));
    setDisplayZone(other);
    const { displayZone: after } = await import("../../web/src/timezone");
    expect(after).toBe(other);
    expect(changes).toBe(1);
    setDisplayZone(other);
    expect(changes).toBe(1);
    setDisplayZone(null);
    const { displayZone: reset } = await import("../../web/src/timezone");
    expect(reset).toBe(browserZone());
  });

  it("ignores a zone the engine cannot format in", async () => {
    setDisplayZone(other);
    setDisplayZone("Mars/Olympus_Mons");
    const { displayZone: after } = await import("../../web/src/timezone");
    expect(after).toBe(browserZone());
  });

  it("looks a point up with the longitude folded back onto the index", async () => {
    const asked: [number, number][] = [];
    useZoneFinder({
      get_tz_name(lng, lat) {
        asked.push([lng, lat]);
        return lng > 100 ? "Asia/Tokyo" : "";
      },
    });
    // 499.77 is 139.77 on a map wrapped once past the antimeridian.
    expect(await zoneAt(499.77, 35.68)).toBe("Asia/Tokyo");
    expect(await zoneAt(0, 0)).toBeNull();
    expect(asked).toEqual([
      [expect.closeTo(139.77, 6), 35.68],
      [0, 0],
    ]);
  });

  it("treats a zone the engine does not know as no answer", async () => {
    useZoneFinder({ get_tz_name: () => "Mars/Olympus_Mons" });
    expect(await zoneAt(0, 0)).toBeNull();
  });

  it("stays on the browser's zone when the lookup could not load", async () => {
    useZoneFinder(null);
    expect(await zoneAt(139.77, 35.68)).toBeNull();
  });
});
