import { describe, expect, it } from "vitest";

import { axisPosition } from "../../web/src/meteogram";
import {
  MODEL_PROFILE_TOLERANCE_MS,
  modelProfileBundles,
  modelProfileLevels,
  nearestFrameForTime,
} from "../../web/src/sounding/model";
import {
  CLOUD_OKTAS,
  OBSERVATION_MATCH_MS,
  cloudCoverPercent,
  cloudOktas,
  formatWind,
  leadSecondsOf,
  nearestObservation,
  rowObservations,
  rowObservedValue,
  rowTakesObservations,
  tafBands,
  type ObservationAxis,
} from "../../web/src/stations/observations";
import type { CloudLayer, Metar, Taf, TafPeriod } from "../../web/src/stations/schema";

const HOUR = 3600 * 1000;

/** A run whose axis is hourly from the analysis out to six hours. */
const AXIS: ObservationAxis = {
  runTimeMs: Date.parse("2026-09-16T00:00:00Z"),
  firstLead: 0,
  lastLead: 6 * 3600,
};

function metar(time: string, values: Partial<Metar> = {}): Metar {
  return {
    time,
    raw: "RJTT 160000Z 27008KT 9999 FEW020 21/20 Q1017",
    t: null,
    td: null,
    wd: null,
    ws: null,
    gust: null,
    vis: null,
    qnh: null,
    slp: null,
    wx: null,
    cloud: [],
    category: null,
    auto: false,
    type: "METAR",
    ...values,
  };
}

function period(values: Partial<TafPeriod> & { from: string; to: string }): TafPeriod {
  return {
    change: null,
    prob: null,
    wd: null,
    ws: null,
    gust: null,
    vis: null,
    wx: null,
    cloud: [],
    ...values,
  };
}

function taf(periods: readonly TafPeriod[]): Taf {
  return {
    issued: "2026-09-15T23:00:00Z",
    from: periods[0]!.from,
    to: periods[periods.length - 1]!.to,
    raw: "TAF RJTT …",
    amended: false,
    periods: [...periods],
  };
}

function layers(...covers: string[]): CloudLayer[] {
  return covers.map((cover, index) => ({ cover, base: 300 * (index + 1) }));
}

describe("modelProfileBundles", () => {
  it("takes the isobaric temperature, humidity and wind a run publishes", () => {
    // What GFS ships today, in the order a manifest happens to list it.
    const bundles = modelProfileBundles([
      "tmp2m",
      "prate",
      "wind10m",
      "prmsl",
      "rh850",
      "tmp500",
      "wind250",
      "tmp850",
      "rh700",
      "wind925",
      "tmp925",
      "rh500",
      "wind850",
    ]);
    expect(bundles.map((bundle) => bundle.id)).toEqual([
      "tmp925",
      "wind925",
      "tmp850",
      "rh850",
      "wind850",
      "rh700",
      "tmp500",
      "rh500",
      "wind250",
    ]);
    expect(bundles[0]).toEqual({ id: "tmp925", level: 925, field: "tmp" });
    expect(bundles.at(-1)).toEqual({ id: "wind250", level: 250, field: "wind" });
  });

  it("leaves out the surface members, which are rows rather than levels", () => {
    expect(modelProfileBundles(["tmp2m", "wind10m", "prmsl", "dpt2m", "gust"])).toEqual([]);
  });

  it("leaves out the families a column has no use for", () => {
    // Geopotential height, specific humidity, vertical velocity and the
    // vapour flux are all published on isobaric surfaces and none of them
    // is temperature, humidity or wind.
    expect(modelProfileBundles(["hgt500", "spfh850", "vvel700", "qflux850", "thetae850"])).toEqual(
      [],
    );
  });

  it("ignores a bundle whose name says nothing", () => {
    expect(modelProfileBundles(["cref", "somethingnew", "tmp"])).toEqual([]);
  });

  it("names each level once, deepest first", () => {
    expect(modelProfileLevels(["tmp850", "rh850", "wind850", "tmp500", "rh500"])).toEqual([850, 500]);
  });
});

describe("nearestFrameForTime", () => {
  // An hourly axis from 00Z.
  const validTimes = [0, 1, 2, 3, 4, 5, 6].map(
    (hour) => Date.parse("2026-09-16T00:00:00Z") + hour * HOUR,
  );

  it("takes the frame at the ascent's own hour", () => {
    expect(nearestFrameForTime(validTimes, Date.parse("2026-09-16T03:00:00Z"))).toBe(3);
  });

  it("takes the nearest when the ascent falls between frames", () => {
    expect(nearestFrameForTime(validTimes, Date.parse("2026-09-16T02:40:00Z"))).toBe(3);
    expect(nearestFrameForTime(validTimes, Date.parse("2026-09-16T02:20:00Z"))).toBe(2);
  });

  it("gives a tie to the earlier frame", () => {
    expect(nearestFrameForTime(validTimes, Date.parse("2026-09-16T02:30:00Z"))).toBe(2);
  });

  it("reaches three hours past the end of the axis and no further", () => {
    expect(MODEL_PROFILE_TOLERANCE_MS).toBe(3 * HOUR);
    expect(nearestFrameForTime(validTimes, Date.parse("2026-09-16T09:00:00Z"))).toBe(6);
    expect(nearestFrameForTime(validTimes, Date.parse("2026-09-16T09:00:01Z"))).toBeNull();
    // And the same before the analysis: a 12Z ascent under a 00Z run of
    // the previous day is nobody's model column.
    expect(nearestFrameForTime(validTimes, Date.parse("2026-09-15T12:00:00Z"))).toBeNull();
  });

  it("answers nothing on an empty axis", () => {
    expect(nearestFrameForTime([], Date.now())).toBeNull();
  });
});

describe("cloud cover", () => {
  it("maps each reported amount to its eighths", () => {
    expect(cloudOktas(layers("FEW"))).toBe(2);
    expect(cloudOktas(layers("SCT"))).toBe(4);
    expect(cloudOktas(layers("BKN"))).toBe(6);
    expect(cloudOktas(layers("OVC"))).toBe(8);
    expect(CLOUD_OKTAS.OVX).toBe(8);
  });

  it("reads a clear sky as none, by every code that says so", () => {
    for (const code of ["CLR", "SKC", "NSC", "CAVOK"]) {
      expect(cloudOktas(layers(code)), code).toBe(0);
    }
  });

  it("takes the highest layer's amount, which is the total", () => {
    expect(cloudOktas(layers("FEW", "SCT", "BKN"))).toBe(6);
    // Order does not matter: the amount does.
    expect(cloudOktas(layers("BKN", "FEW"))).toBe(6);
  });

  it("says nothing about a sky the report did not describe", () => {
    expect(cloudOktas([])).toBeNull();
    expect(cloudOktas(layers("WHAT"))).toBeNull();
    expect(cloudCoverPercent([])).toBeNull();
  });

  it("draws in the percent the cloud row is scaled in", () => {
    expect(cloudCoverPercent(layers("OVC"))).toBe(100);
    expect(cloudCoverPercent(layers("SCT"))).toBe(50);
    expect(cloudCoverPercent(layers("CLR"))).toBe(0);
  });
});

describe("observation placement", () => {
  it("places a report at its own lead seconds", () => {
    expect(leadSecondsOf("2026-09-16T00:00:00Z", AXIS)).toBe(0);
    expect(leadSecondsOf("2026-09-16T02:30:00Z", AXIS)).toBe(9000);
    expect(leadSecondsOf("2026-09-16T06:00:00Z", AXIS)).toBe(21600);
  });

  it("drops a report outside the run's span rather than clamping it", () => {
    expect(leadSecondsOf("2026-09-15T23:30:00Z", AXIS)).toBeNull();
    expect(leadSecondsOf("2026-09-16T06:00:01Z", AXIS)).toBeNull();
    expect(leadSecondsOf("not a time", AXIS)).toBeNull();
  });

  it("puts the temperature row's marks at their reports, oldest first", () => {
    const marks = rowObservations(
      "temperature",
      [
        metar("2026-09-16T03:00:00Z", { t: 21.5, td: 18 }),
        metar("2026-09-16T01:00:00Z", { t: 19.5, td: 17 }),
        // Before the run: no column for it.
        metar("2026-09-15T22:00:00Z", { t: 18, td: 16 }),
      ],
      AXIS,
    );
    expect(marks.map((mark) => [mark.x, mark.y, mark.kind])).toEqual([
      [3600, 19.5, "dot"],
      [3600, 17, "hollow"],
      [10800, 21.5, "dot"],
      [10800, 18, "hollow"],
    ]);
  });

  it("draws a gust as a tick beside the wind's dot, and skips what is not reported", () => {
    const marks = rowObservations(
      "wind",
      [metar("2026-09-16T02:00:00Z", { ws: 6.7, gust: 12.3 }), metar("2026-09-16T04:00:00Z", { ws: 5.1 })],
      AXIS,
    );
    expect(marks.map((mark) => mark.kind)).toEqual(["dot", "tick", "dot"]);
    expect(marks[1]!.y).toBe(12.3);
  });

  it("falls back from the sea-level pressure to the altimeter setting", () => {
    const withSlp = rowObservations("pressure", [metar("2026-09-16T01:00:00Z", { slp: 1016.4, qnh: 1016.9 })], AXIS);
    expect(withSlp[0]!.y).toBe(1016.4);
    const withoutSlp = rowObservations("pressure", [metar("2026-09-16T01:00:00Z", { qnh: 1016.9 })], AXIS);
    expect(withoutSlp[0]!.y).toBe(1016.9);
  });

  it("draws the sky as a stepped cover", () => {
    const marks = rowObservations(
      "cloud",
      [metar("2026-09-16T01:00:00Z", { cloud: layers("BKN") })],
      AXIS,
    );
    expect(marks).toEqual([{ x: 3600, y: 75, kind: "step" }]);
  });

  it("has nothing to put on the precipitation row, which reports no rate", () => {
    expect(rowTakesObservations("precipitation")).toBe(false);
    expect(rowObservations("precipitation", [metar("2026-09-16T01:00:00Z")], AXIS)).toEqual([]);
    expect(rowTakesObservations("temperature")).toBe(true);
  });

  it("names the one value that belongs beside a row's readout", () => {
    const report = metar("2026-09-16T01:00:00Z", { t: 21.5, td: 18, ws: 6.7, gust: 12 });
    expect(rowObservedValue("temperature", report)).toBe(21.5);
    expect(rowObservedValue("wind", report)).toBe(6.7);
    expect(rowObservedValue("precipitation", report)).toBeNull();
    expect(rowObservedValue("pressure", report)).toBeNull();
  });
});

describe("nearestObservation", () => {
  const history = [
    metar("2026-09-16T03:00:00Z", { t: 22 }),
    metar("2026-09-16T02:30:00Z", { t: 21 }),
    metar("2026-09-16T00:00:00Z", { t: 19 }),
  ];

  it("takes the report nearest the frame", () => {
    expect(nearestObservation(history, Date.parse("2026-09-16T02:50:00Z"))?.t).toBe(22);
    expect(nearestObservation(history, Date.parse("2026-09-16T02:35:00Z"))?.t).toBe(21);
  });

  it("stops at ninety minutes", () => {
    expect(OBSERVATION_MATCH_MS).toBe(90 * 60 * 1000);
    // 90 minutes past the newest report: still paired.
    expect(nearestObservation(history, Date.parse("2026-09-16T04:30:00Z"))?.t).toBe(22);
    // One second more, and the station has nothing to say about this frame.
    expect(nearestObservation(history, Date.parse("2026-09-16T04:30:01Z"))).toBeNull();
  });

  it("answers nothing for a station with no reports", () => {
    expect(nearestObservation([], Date.now())).toBeNull();
  });
});

describe("tafBands", () => {
  it("lays each period on the axis, clipped to it", () => {
    const bands = tafBands(
      taf([
        period({ from: "2026-09-15T23:00:00Z", to: "2026-09-16T02:00:00Z", wd: 270, ws: 8 }),
        period({ from: "2026-09-16T02:00:00Z", to: "2026-09-16T09:00:00Z", change: "FM", wd: 180, ws: 5 }),
      ]),
      AXIS,
    );
    expect(bands).toHaveLength(2);
    // The prevailing group starts before the analysis and the last runs past
    // the final frame; both are cut to the axis rather than dropped.
    expect(bands[0]!.from).toBe(0);
    expect(bands[0]!.to).toBe(2 * 3600);
    expect(bands[1]!.to).toBe(6 * 3600);
    expect(bands.map((band) => band.style)).toEqual(["solid", "solid"]);
    expect(bands[0]!.label).toBe("270° 8 m/s");
    expect(bands[1]!.label).toBe("180° 5 m/s");
  });

  it("hatches a TEMPO group and carries its probability", () => {
    const bands = tafBands(
      taf([
        period({ from: "2026-09-16T00:00:00Z", to: "2026-09-16T06:00:00Z", wd: 270, ws: 8 }),
        period({
          from: "2026-09-16T02:00:00Z",
          to: "2026-09-16T04:00:00Z",
          change: "TEMPO",
          prob: 40,
          wx: "-SHRA",
        }),
      ]),
      AXIS,
    );
    expect(bands[1]!.style).toBe("hatched");
    expect(bands[1]!.prob).toBe(40);
    // The group states only the weather, so it inherits the prevailing
    // group's wind exactly as a reader of the bulletin would.
    expect(bands[1]!.label).toBe("270° 8 m/s -SHRA");
  });

  it("hatches a BECMG group no more than an FM one", () => {
    const bands = tafBands(
      taf([
        period({ from: "2026-09-16T00:00:00Z", to: "2026-09-16T03:00:00Z", wd: 90, ws: 3 }),
        period({ from: "2026-09-16T03:00:00Z", to: "2026-09-16T05:00:00Z", change: "BECMG", wd: 200, ws: 9 }),
      ]),
      AXIS,
    );
    expect(bands.map((band) => band.style)).toEqual(["solid", "solid"]);
  });

  it("drops a period wholly outside the axis, and a zero-length one", () => {
    const bands = tafBands(
      taf([
        period({ from: "2026-09-16T08:00:00Z", to: "2026-09-16T12:00:00Z", wd: 10, ws: 4 }),
        period({ from: "2026-09-16T01:00:00Z", to: "2026-09-16T01:00:00Z", wd: 10, ws: 4 }),
      ]),
      AXIS,
    );
    expect(bands).toEqual([]);
  });

  it("writes a variable direction as the code the report uses", () => {
    expect(formatWind(null, 3)).toBe("VRB 3 m/s");
    expect(formatWind(90, 7.4)).toBe("090° 7 m/s");
    expect(formatWind(90, null)).toBeNull();
  });
});

describe("axisPosition", () => {
  // The GFS shape: hourly to three hours, three-hourly after it.
  const leads = [0, 3600, 7200, 10800, 21600, 32400];

  it("places a frame's own lead time on its column", () => {
    expect(axisPosition(leads, 0)).toBe(0);
    expect(axisPosition(leads, 10800)).toBe(3);
    expect(axisPosition(leads, 32400)).toBe(5);
  });

  it("places an instant between two frames at the fraction between them", () => {
    expect(axisPosition(leads, 1800)).toBeCloseTo(0.5, 9);
    // Halfway through a three-hourly step is still half a column, since the
    // columns are placed by index rather than by time.
    expect(axisPosition(leads, 16200)).toBeCloseTo(3.5, 9);
  });

  it("has no place for an instant off either end", () => {
    expect(axisPosition(leads, -1)).toBeNull();
    expect(axisPosition(leads, 32401)).toBeNull();
    expect(axisPosition([], 0)).toBeNull();
  });

  it("puts everything on the one column of a single-frame axis", () => {
    expect(axisPosition([600], 600)).toBe(0);
    expect(axisPosition([600], 0)).toBeNull();
  });
});
