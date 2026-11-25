import { describe, expect, it } from "vitest";

import { DEFAULT_POINT, defaultState, nearestFrame, nearestLevel, parseState, probeOnLine, searchForState, stepLevel } from "../../../web/src/workbench/state";
import type { SectionPoint } from "../../../web/src/crosssection/types";

const LINE = "?section=39.90,116.40,35.70,139.70";

describe("the workbench's address", () => {
  it("is a point by default, with nothing to say of it but where", () => {
    expect(parseState("")).toEqual(defaultState());
    expect(searchForState(defaultState(), "")).toBe("?point=35.68,139.69");
  });

  it("comes back as it was written", () => {
    for (const search of [
      "?point=36.60,136.64",
      `${LINE}&point=36.60,136.64&time=2026-11-27T18Z&p=700&fill=rh&top=500&span=240`,
      `${LINE}&fill=cross`,
      "?model=ecmwf&point=-33.90,-151.20&p=500",
    ]) {
      const written = searchForState(parseState(search), "");
      expect(parseState(written)).toEqual(parseState(search));
      expect(searchForState(parseState(written), "")).toBe(written);
    }
    expect(parseState(`${LINE}&time=2026-11-27T18Z`).time).toBe(Date.UTC(2026, 10, 27, 18));
  });

  it("keeps the parameters that are not its own", () => {
    expect(searchForState(parseState("?theme=dark&p=700"), "?theme=dark&p=700")).toBe("?theme=dark&point=35.68,139.69&p=700");
  });

  it("counts what does not parse as not there", () => {
    const broken = "?model=nosuch&section=1,2,3&point=95,10&time=2026-02-30T00Z&p=high&fill=mauve&top=50&span=7";
    expect(parseState(broken)).toEqual(defaultState());
    expect(parseState("?time=2026-11-27T18:00Z").time).toBeNull();
    expect(parseState("?p=0").level).toBe(850);
    expect(parseState("?model=constructor").model).toBe("gfs");
  });

  it("leaves a line's probe to its plan where the link names none", () => {
    expect(parseState(LINE).point).toBeNull();
    expect(parseState("?section=bad").point).toEqual(DEFAULT_POINT);
  });

  it("has no wind across a line where there is none", () => {
    expect(parseState("?fill=cross").fill).toBe("wind");
    expect(parseState(`${LINE}&fill=cross`).fill).toBe("cross");
  });

  it("brings a longitude into one turn", () => {
    expect(parseState("?point=10,190").point).toEqual([-170, 10]);
  });
});

describe("the coordinates on their axes", () => {
  const points = Array.from({ length: 11 }, (_, at): SectionPoint => ({ lon: 100 + at, lat: 0, along: at * 111_000, bearing: 0 }));

  it("has the probe at the nearest point of the line, or its middle", () => {
    expect(probeOnLine(points, [103.2, 0.1])).toBe(3);
    expect(probeOnLine(points, [103.2, 5])).toBe(5);
    expect(probeOnLine(points, null)).toBe(5);
  });

  it("has the nearest frame, and says where a time is off the axis", () => {
    const offsets = [0, 3600, 7200, 18_000];
    expect(nearestFrame(offsets, 0, 3_700_000)).toEqual({ frame: 1, outside: false });
    expect(nearestFrame(offsets, 0, 13_000_000)).toEqual({ frame: 3, outside: false });
    expect(nearestFrame(offsets, 0, -1)).toEqual({ frame: 0, outside: true });
    expect(nearestFrame(offsets, 0, 18_000_001)).toEqual({ frame: 3, outside: true });
    expect(nearestFrame([], 0, 0)).toEqual({ frame: 0, outside: true });
  });

  it("steps through the levels on the chart", () => {
    const levels = [1000, 850, 700, 500, 300, 100];
    expect(nearestLevel(levels, 780)).toBe(850);
    expect(nearestLevel(levels, 770)).toBe(700);
    expect(stepLevel(levels, 700, 1, 100)).toBe(500);
    expect(stepLevel(levels, 700, -1, 100)).toBe(850);
    expect(stepLevel(levels, 500, 1, 500)).toBeNull();
    expect(stepLevel(levels, 1000, -1, 100)).toBeNull();
    // From above a chart that has since been cut short.
    expect(stepLevel(levels, 300, -1, 500)).toBe(500);
    expect(stepLevel(levels, 300, 1, 500)).toBeNull();
  });
});
