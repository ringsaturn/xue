import { describe, expect, it } from "vitest";

import { P_BOTTOM } from "../../web/src/crosssection/chart";
import {
  SectionDeriver,
  acrossLine,
  equivalentPotentialTemperature,
  potentialTemperature,
  relativeHumidity,
  roundHeights,
  smoothAlong,
} from "../../web/src/crosssection/derive";
import { SECTION_ARRAYS, SectionField, type FieldSpec, type SectionArrayId } from "../../web/src/crosssection/field";
import { modelColumn, surfacePressure } from "../../web/src/sounding/column";
import { dewPointFromRelativeHumidity } from "../../web/src/sounding/profile";

const LEVELS = [1000, 850, 700, 500, 300, 200, 100];

/** One column of a run, by array; the same at every point and frame. */
const COLUMN: Record<SectionArrayId, number[]> = {
  tmp: [18, 8, -1, -18, -44, -56, -60],
  rh: [80, 60, 50, 30, 20, 10, 5],
  hgt: [110, 1490, 3050, 5650, 9300, 11900, 16300],
  ugrd: [2, 6, 10, 20, 35, 40, 15],
  vgrd: [1, 2, 3, 4, 5, 6, 2],
  tmp2m: [20],
  dpt2m: [16],
  prmsl: [1013],
  orog: [30],
  ugrd10m: [1],
  vgrd10m: [0],
};

/** A field of `points` points whose every value is the column's: code 0 is it. */
function field(points: number, column: Partial<Record<SectionArrayId, number[]>>, frames = 1): SectionField {
  const spec: FieldSpec = {
    arrays: SECTION_ARRAYS.filter((id) => column[id]).map((id) => {
      const values = column[id]!;
      const levelled = values.length > 1;
      return {
        id,
        levels: levelled ? LEVELS : null,
        offsets: Array.from({ length: id === "orog" ? 1 : frames }, (_, frame) => frame * 3600),
        tables: Float32Array.from({ length: values.length * 256 }, (_, at) => values[at >> 8]!),
      };
    }),
    points,
    cells: points,
    corners: Uint32Array.from({ length: points * 4 }, (_, at) => at >> 2),
    weights: new Float32Array(points * 4).fill(0.25),
    bearings: new Float64Array(points),
    latitudes: new Float64Array(points),
  };
  return new SectionField(spec);
}

function fill(target: SectionField, ids: readonly SectionArrayId[] = SECTION_ARRAYS, points = target.spec.points): void {
  for (const id of ids) {
    const array = target.array(id);
    if (!array) continue;
    const size = array.offsets.length * (array.levels?.length ?? 1);
    target.add(id, Uint32Array.from({ length: points }, (_, cell) => cell), new Uint8Array(points * size));
  }
}

const PS = surfacePressure(
  LEVELS.map((p, at) => ({ p, z: COLUMN.hgt[at]! })),
  1013,
  30,
)!;

describe("the thermodynamics", () => {
  it("has the dry quantities at their reference", () => {
    expect(potentialTemperature(20, 1000)).toBeCloseTo(293.15, 6);
    expect(potentialTemperature(-20, 500)).toBeCloseTo(308.53, 2);
    expect(relativeHumidity(20, 20)).toBe(100);
    expect(relativeHumidity(20, 21)).toBe(100);
    expect(relativeHumidity(20, dewPointFromRelativeHumidity(20, 37))).toBeCloseTo(37, 6);
    expect(relativeHumidity(20, Number.NaN)).toBeNaN();
  });

  it("has Bolton's equivalent potential temperature", () => {
    expect(equivalentPotentialTemperature(20, 18, 850)).toBeCloseTo(353.9, 0);
    // Cold dry air carries next to no vapour.
    expect(equivalentPotentialTemperature(-60, -80, 200) - potentialTemperature(-60, 200)).toBeLessThan(0.1);
  });

  it("counts the wind across a line out of the page, its start at the left", () => {
    // Northward: a westerly comes towards the reader, who looks west.
    expect(acrossLine(10, 5, 0)).toBeCloseTo(10, 9);
    // Eastward: the reader looks north, and a northerly comes out.
    expect(acrossLine(5, -10, Math.PI / 2)).toBeCloseTo(10, 9);
  });
});

describe("SectionDeriver", () => {
  it("stands a column on its ground", () => {
    const target = field(3, COLUMN);
    fill(target);
    const slice = new SectionDeriver(target).slice(0, 100, ["t", "td", "wind"])!;
    expect(slice.ps[1]).toBeCloseTo(PS, 3);
    expect([...slice.drawn]).toEqual([1, 1, 1]);
    const t = slice.quantity("t");
    // Under the ground the 2 m value is held.
    expect(t[1]).toBeCloseTo(20, 5);
    // The top row is the top level.
    expect(t[(slice.rows - 1) * 3 + 1]).toBeCloseTo(-60, 4);
    // Linear in ln p between two levels.
    const row = slice.rowP.findIndex((p) => p < 400);
    const share = Math.log(500 / slice.rowP[row]!) / Math.log(500 / 300);
    expect(t[row * 3 + 1]).toBeCloseTo(-18 + share * (-44 + 18), 3);
    expect(slice.rowP[0]).toBeCloseTo(P_BOTTOM, 9);
  });

  it("leaves out the levels the run extrapolated under the ground", () => {
    const target = field(2, { ...COLUMN, orog: [2000] });
    fill(target);
    const deriver = new SectionDeriver(target);
    const slice = deriver.slice(0, 100, ["t"])!;
    expect(slice.ps[0]).toBeLessThan(810);
    expect(slice.ps[0]).toBeGreaterThan(780);
    // 20 °C at the ground, not the 8 °C of the 850 hPa under it.
    const row = slice.rowP.findIndex((p) => p < 850);
    expect(slice.quantity("t")[row * 2]).toBeCloseTo(20, 5);
    const under = deriver.reading(0, 0, 850)!;
    expect(under.belowGround).toBe(true);
    expect(under.surfacePressure).toBeCloseTo(slice.ps[0]!, 2);
    expect(under.temperature).toBeNaN();
  });

  it("draws a point once everything the groups need of it is here", () => {
    const target = field(2, COLUMN);
    fill(target, ["hgt", "orog", "prmsl", "tmp"]);
    const deriver = new SectionDeriver(target);
    expect([...deriver.slice(0, 100, ["t"])!.drawn]).toEqual([0, 0]);
    expect(deriver.slice(0, 100, ["t"])!.quantity("theta")[0]).toBeNaN();
    fill(target, ["tmp2m"], 1);
    expect([...deriver.slice(0, 100, ["t"])!.drawn]).toEqual([1, 0]);
    expect([...deriver.slice(0, 100, ["t", "wind"])!.drawn]).toEqual([0, 0]);
  });

  it("does not wait for a surface field the store has not got", () => {
    const { tmp2m: _, ...rest } = COLUMN;
    const target = field(1, rest);
    fill(target);
    const slice = new SectionDeriver(target).slice(0, 100, ["t"])!;
    expect([...slice.drawn]).toEqual([1]);
    // The column begins at its lowest level.
    expect(slice.quantity("t")[0]).toBeCloseTo(18, 5);
  });

  it("has nothing above a column's top level", () => {
    const target = field(1, COLUMN);
    fill(target);
    const deriver = new SectionDeriver(target);
    const slice = deriver.slice(0, 50, ["t"])!;
    expect(slice.quantity("t")[slice.rows - 1]).toBeNaN();
    expect(deriver.reading(0, 0, 60)!.temperature).toBeNaN();
  });

  it("has no slice where the store has no frame", () => {
    const target = field(1, COLUMN, 2);
    fill(target);
    const deriver = new SectionDeriver(target);
    expect(deriver.slice(3600, 100, ["t"])).not.toBeNull();
    expect(deriver.slice(3601, 100, ["t"])).toBeNull();
    expect(deriver.reading(3601, 0, 500)).toBeNull();
  });

  it("keeps a frame until something arrives, and no more than three", () => {
    const target = field(1, COLUMN, 5);
    fill(target);
    const deriver = new SectionDeriver(target);
    const first = deriver.slice(0, 100, ["t"]);
    expect(deriver.slice(1800, 100, ["t"])).toBe(first);
    for (const frame of [1, 2, 3]) deriver.slice(frame * 3600, 100, ["t"]);
    // Its place was taken by the fourth.
    expect(deriver.slice(3 * 3600, 100, ["t"])).toBe(first);
  });

  it("reads a point where it is asked", () => {
    const target = field(2, COLUMN);
    fill(target);
    const reading = new SectionDeriver(target).reading(0, 1, 500)!;
    expect(reading.belowGround).toBe(false);
    expect(reading.height).toBeCloseTo(5650, 2);
    expect(reading.temperature).toBeCloseTo(-18, 4);
    expect(reading.humidity).toBeCloseTo(30, 3);
    expect(reading.windSpeed).toBeCloseTo(Math.hypot(20, 4), 4);
    expect(reading.windDirection).toBeCloseTo(258.69, 1);
    expect(reading.theta).toBeCloseTo(potentialTemperature(-18, 500), 3);
    expect(reading.thetaE).toBeGreaterThan(reading.theta);
  });

  it("reads nothing of a point whose ground is not here yet", () => {
    const target = field(2, COLUMN);
    const deriver = new SectionDeriver(target);
    expect(deriver.reading(0, 0, 500)).toBeNull();
    fill(target, ["tmp"]);
    expect(deriver.reading(0, 0, 500)).toBeNull();
    fill(target, ["hgt", "orog", "prmsl"]);
    const reading = deriver.reading(0, 0, 500)!;
    expect(reading.temperature).toBeCloseTo(-18, 4);
    expect(reading.windSpeed).toBeNaN();
  });

  it("places the multiples of a step on the pressure axis", () => {
    const target = field(2, COLUMN);
    fill(target);
    const heights = roundHeights(new SectionDeriver(target).slice(0, 100, ["t"])!, 1000);
    expect(heights.map(({ metres }) => metres)).toEqual(Array.from({ length: 16 }, (_, at) => (at + 1) * 1000));
    const three = heights[2]!.pressure;
    expect(three).toBeGreaterThan(700);
    expect(three).toBeLessThan(710);
    expect(heights.every(({ pressure }, at) => at === 0 || pressure < heights[at - 1]!.pressure)).toBe(true);
    const tenThousandFeet = roundHeights(new SectionDeriver(target).slice(0, 100, ["t"])!, 3048)[0]!;
    expect(tenThousandFeet.metres).toBe(3048);
    expect(tenThousandFeet.pressure).toBeLessThan(three);
  });
});

/** Hourly to three hours, then every three. */
const HOURS = [0, 1, 2, 3, 6, 9];

/** A field whose every value is the column's plus its frame's number, on an
 * axis that changes its step. A point faces its own way. */
function changing(points: number): SectionField {
  const spec: FieldSpec = {
    ...field(points, COLUMN).spec,
    arrays: SECTION_ARRAYS.map((id) => {
      const values = COLUMN[id];
      return {
        id,
        levels: values.length > 1 ? LEVELS : null,
        offsets: id === "orog" ? [0] : HOURS.map((hour) => hour * 3600),
        tables: Float32Array.from({ length: values.length * 256 }, (_, at) => values[at >> 8]! + (at & 255)),
      };
    }),
    bearings: Float64Array.from({ length: points }, (_, point) => point),
    latitudes: Float64Array.from({ length: points }, (_, point) => -10 * point),
  };
  return new SectionField(spec);
}

function fillChanging(target: SectionField, ids: readonly SectionArrayId[] = SECTION_ARRAYS, points = target.spec.points): void {
  for (const id of ids) {
    const array = target.array(id)!;
    const frames = array.offsets.length;
    const members = array.levels?.length ?? 1;
    const codes = Uint8Array.from({ length: points * frames * members }, (_, at) => Math.floor(at / members) % frames);
    target.add(id, Uint32Array.from({ length: points }, (_, cell) => cell), codes);
  }
}

const ALL = ["t", "td", "wind"] as const;
const BASES = ["t", "td", "u", "v", "rh", "theta", "thetae", "wind", "cross"] as const;

describe("a point through time", () => {
  const columns = { first: 0, step: 3600, count: 10 };

  it("has a frame's own values in the frame's column", () => {
    const target = changing(3);
    fillChanging(target);
    const deriver = new SectionDeriver(target);
    const through = deriver.timeSlice(2, columns, 100, ALL);
    for (const hour of HOURS) {
      const frame = deriver.slice(hour * 3600, 100, ALL)!;
      expect(through.ps[hour]).toBe(frame.ps[2]);
      for (const quantity of BASES) {
        const time = through.quantity(quantity);
        const line = frame.quantity(quantity);
        for (let row = 0; row < frame.rows; row += 1) {
          expect(Object.is(time[row * 10 + hour], line[row * 3 + 2]), `${quantity} ${hour} ${row}`).toBe(true);
        }
      }
    }
    expect([...through.latitudes]).toEqual(Array(10).fill(-20));
  });

  it("is linear between two frames", () => {
    const target = changing(1);
    fillChanging(target);
    const through = new SectionDeriver(target).timeSlice(0, columns, 100, ["t"]);
    const t = through.quantity("t");
    const row = 90 * 10;
    expect(t[row + 4]).toBeCloseTo(t[row + 3]! + (t[row + 6]! - t[row + 3]!) / 3, 4);
    expect(t[row + 8]).toBeCloseTo(t[row + 6]! + ((t[row + 9]! - t[row + 6]!) * 2) / 3, 4);
    expect(through.ps[5]).toBeCloseTo(through.ps[3]! + ((through.ps[6]! - through.ps[3]!) * 2) / 3, 3);
  });

  it("has the same columns however they are stepped", () => {
    const target = changing(1);
    fillChanging(target);
    const deriver = new SectionDeriver(target);
    const t = Float32Array.from(deriver.timeSlice(0, columns, 100, ["t"]).quantity("t"));
    const sparse = deriver.timeSlice(0, { first: 7200, step: 7200, count: 4 }, 100, ["t"]);
    for (let column = 0; column < 4; column += 1) expect(sparse.quantity("t")[90 * 4 + column]).toBe(t[90 * 10 + 2 + 2 * column]);
  });

  it("has nothing past the axis, nor before everything of the point is here", () => {
    const target = changing(2);
    const deriver = new SectionDeriver(target);
    fillChanging(target, ["hgt", "orog", "prmsl", "tmp"]);
    expect([...deriver.timeSlice(0, columns, 100, ["t"]).drawn]).toEqual(Array(10).fill(0));
    fillChanging(target, ["tmp2m"], 1);
    const through = deriver.timeSlice(0, { first: -3600, step: 3600, count: 12 }, 100, ["t"]);
    expect([...through.drawn]).toEqual([0, ...Array(10).fill(1), 0]);
    expect(through.ps[0]).toBeNaN();
    expect(through.quantity("theta")[11]).toBeNaN();
    expect(deriver.timeSlice(1, columns, 100, ["t"]).drawn[0]).toBe(0);
  });

  it("keeps what it last derived", () => {
    const target = changing(2);
    fillChanging(target);
    const deriver = new SectionDeriver(target);
    const first = deriver.timeSlice(0, columns, 100, ["t"]);
    const { key } = first;
    expect(deriver.timeSlice(0, columns, 100, ["t"])).toBe(first);
    expect(deriver.timeSlice(1, columns, 100, ["t"]).key).not.toBe(key);
  });
});

describe("a column to lift a parcel in", () => {
  it("has the values the section is read with", () => {
    const target = changing(2);
    fillChanging(target);
    const deriver = new SectionDeriver(target);
    const read = deriver.columnAt(2 * 3600, 1)!;
    expect(read.levels.map(({ p }) => p)).toEqual(LEVELS);
    const column = modelColumn(read.levels, read.surface);
    for (const p of [850, 700, 500, 300, 200, 100]) {
      const reading = deriver.reading(2 * 3600, 1, p)!;
      const at = column.profile.p.indexOf(p);
      expect(column.profile.t[at]).toBeCloseTo(reading.temperature, 4);
      expect(relativeHumidity(column.profile.t[at]!, column.profile.td[at]!)).toBeCloseTo(reading.humidity, 4);
      expect(column.profile.ws[at]).toBeCloseTo(reading.windSpeed, 4);
      expect(column.profile.wd[at]).toBeCloseTo(reading.windDirection, 4);
      expect(column.profile.z[at]).toBeCloseTo(reading.height, 2);
    }
    expect(column.pSurface).toBeCloseTo(deriver.reading(2 * 3600, 1, 500)!.surfacePressure, 4);
    expect(read.surface).toEqual({ t: 22, td: 18, u: 3, v: 2, prmsl: 1015, orog: 30 });
  });

  it("is none before the ground is here and past the axis, and lacks what has not arrived", () => {
    const target = changing(1);
    const deriver = new SectionDeriver(target);
    expect(deriver.columnAt(0, 0)).toBeNull();
    fillChanging(target, ["tmp"]);
    expect(deriver.columnAt(0, 0)).toBeNull();
    fillChanging(target, ["hgt", "orog", "prmsl"]);
    const read = deriver.columnAt(0, 0)!;
    expect(read.levels[1]).toEqual({ p: 850, z: 1490, t: 8 });
    expect(read.surface).toEqual({ t: null, td: null, u: null, v: null, prmsl: 1013, orog: 30 });
    expect(deriver.columnAt(9 * 3600 + 1, 0)).toBeNull();
    expect(deriver.columnAt(0, 1)).toBeNull();
  });
});

describe("smoothAlong", () => {
  it("smooths a row of its own and leaves a line as it is", () => {
    const values = Float32Array.from([0, 0, 16, 0, 0, 1, 2, 3, 4, 5]);
    const out = new Float32Array(10);
    smoothAlong(values, 5, out);
    expect([...out.subarray(0, 5)]).toEqual([1, 4, 6, 4, 1]);
    expect([...out.subarray(5)].slice(2, 3)).toEqual([3]);
  });

  it("takes nothing from a missing neighbour", () => {
    const out = new Float32Array(3);
    smoothAlong(Float32Array.from([4, Number.NaN, 8]), 3, out);
    expect(out[0]).toBe(4);
    expect(out[1]).toBeNaN();
    expect(out[2]).toBe(8);
  });
});
