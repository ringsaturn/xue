import { describe, expect, it } from "vitest";

import { parseState } from "../../web/src/workbench/state";
import { workbenchUrl } from "../../web/src/workbenchlink";

const TIME = Date.UTC(2026, 10, 27, 18);

describe("a link into the workbench", () => {
  it("opens on the place, the time and the drawing it was made of", () => {
    const line = { a: [116.4, 39.9], b: [139.7, 35.7] } as { a: [number, number]; b: [number, number] };
    const href = workbenchUrl("gfs", { line, time: TIME, fill: "rh", top: 500 });
    expect(href).toBe("/workbench?section=39.90,116.40,35.70,139.70&time=2026-11-27T18Z&fill=rh&top=500");
    expect(parseState(href.slice(href.indexOf("?")))).toMatchObject({ model: "gfs", line, time: TIME, fill: "rh", top: 500 });
  });

  it("leaves the defaults out and brings a point into one turn", () => {
    expect(workbenchUrl("gfs", { point: [496.6449, 36.6], time: null, fill: "thetae", top: 100 })).toBe("/workbench?point=36.60,136.64");
    expect(workbenchUrl("ecmwf", { point: [10, 5], time: TIME + 600_000 })).toBe("/workbench?model=ecmwf&point=5.00,10.00&time=2026-11-27T18Z");
  });
});
