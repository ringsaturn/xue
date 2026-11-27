import { describe, expect, it } from "vitest";

import { frameOffsets, parseBundleMetadata, type BundleTimeAxis, type LinearQuantization } from "../../web/src/manifest";
import { alignSeries, seriesState } from "../../web/src/meteogram";
import type { VariableSession } from "../../web/src/session";
import { sessionOffsetForLead } from "../../web/src/sessions";
import { globalGrid, metadataJson, registryEntries, registryVariable } from "./helpers";

// ECMWF's run axis is three-hourly to F144 and six-hourly to F240; the IFS
// ENS precipitation probabilities on it are 24-hour windows every 12 hours
// from F024, on a `time_<id>` axis of their own. The axis is not a suffix of
// the run's: most run frames have no probability at all.

const HOUR = 3600;

/** The ecmwf run axis, F000–F240, as lead hours. */
const RUN_HOURS = [
  ...Array.from({ length: 49 }, (_, index) => index * 3),
  ...Array.from({ length: 16 }, (_, index) => 150 + index * 6),
];
/** The probabilities' own axis: the window ending at 24, 36, …, 240. */
const PROBABILITY_HOURS = Array.from({ length: 19 }, (_, index) => 24 + index * 12);

const RUN_AXIS: BundleTimeAxis = { frameCount: RUN_HOURS.length, unitSeconds: HOUR, frameOffsets: RUN_HOURS };
const PROBABILITY_AXIS: BundleTimeAxis = { frameCount: 19, unitSeconds: HOUR, firstFrameOffset: 24, frameStep: 12 };

function session(time: BundleTimeAxis): VariableSession {
  return { metadata: { ...globalGrid(), time }, leadOffsets: null } as unknown as VariableSession;
}

describe("a probability on a twelve-hourly axis of its own", () => {
  it("parses as a uniform axis from the 24th hour", () => {
    const entry = registryEntries<LinearQuantization>(["apcp24gt10"]).apcp24gt10!;
    const variable = registryVariable("apcp24gt10", entry, entry.quality, { ensemble: entry.ensemble });
    const json = metadataJson(
      {
        model: "ECMWF",
        runTime: "2026-11-26T00:00:00Z",
        time: PROBABILITY_AXIS,
        grid: { width: 1440, height: 721, firstLongitude: -180, firstLatitude: 90, longitudeStep: 0.25, latitudeStep: -0.25, wrapLongitude: true },
      },
      [variable],
    );
    expect(frameOffsets(parseBundleMetadata(json).time)).toEqual(PROBABILITY_HOURS);
  });

  it("is a subset of the run axis, so lines over it follow every frame", () => {
    // The pressure lines drawn over a probability fill follow the fill's
    // frames by lead: each must exist on the run's axis.
    const lines = session(RUN_AXIS);
    for (const hour of PROBABILITY_HOURS) expect(sessionOffsetForLead(lines, hour * HOUR), `F${hour}`).toBe(hour);
  });

  it("has no frame between its windows, never a neighbour's", () => {
    const probability = session(PROBABILITY_AXIS);
    expect(sessionOffsetForLead(probability, 24 * HOUR)).toBe(24);
    expect(sessionOffsetForLead(probability, 27 * HOUR)).toBeNull();
    expect(sessionOffsetForLead(probability, 30 * HOUR)).toBeNull();
    expect(sessionOffsetForLead(probability, 0)).toBeNull();
    expect(sessionOffsetForLead(probability, 240 * HOUR)).toBe(240);
  });

  it("reads into a pin's rows as a gap off its windows, and is complete with them", () => {
    const probability = session(PROBABILITY_AXIS);
    const leads = RUN_HOURS.map((hour) => hour * HOUR);
    const offsetFor = (lead: number) => sessionOffsetForLead(probability, lead);
    const values = alignSeries(leads, offsetFor, (offset) => offset);
    const read = RUN_HOURS.filter((_, index) => values[index] !== undefined);
    expect(read).toEqual(PROBABILITY_HOURS);
    expect(seriesState(leads, offsetFor, values)).toBe("complete");
  });
});
