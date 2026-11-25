import { describe, expect, it, vi } from "vitest";

import { P_BOTTOM, axisPressures, pointX, pressureY, xPoint, yPressure } from "../../web/src/crosssection/chart";
import { DrawBuffers, barbLevels, paintFill } from "../../web/src/crosssection/draw";
import { SectionEngine, type SectionReply } from "../../web/src/crosssection/engine";
import { sectionBands, sectionLegend } from "../../web/src/crosssection/ramps";
import { drawBarb } from "../../web/src/sounding/skewt";
import { SectionRenderer, fieldSpec } from "../../web/src/crosssection/renderer";
import type { SectionDrawState, SectionFill, SectionPlan, SectionStore } from "../../web/src/crosssection/types";
import type { ColumnQuantity } from "../../web/src/identity";
import type { GridMap } from "../../web/src/gridmap";
import type { BundleVariable } from "../../web/src/manifest";

const LEVELS = [1000, 975, 950, 925, 900, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100];
const FILLS: SectionFill[] = ["thetae", "tmp", "rh", "wind", "cross"];

describe("the chart's geometry", () => {
  it("runs from the bottom pressure to the top's, and back", () => {
    expect(pressureY(P_BOTTOM, 100, 300)).toBeCloseTo(300, 9);
    expect(pressureY(100, 100, 300)).toBe(0);
    for (const pressure of [1000, 700, 320, 101]) expect(yPressure(pressureY(pressure, 100, 240), 100, 240)).toBeCloseTo(pressure, 6);
    expect(xPoint(pointX(37, 151, 900), 151, 900)).toBe(37);
    expect(xPoint(-5, 151, 900)).toBe(0);
    expect(xPoint(905, 151, 900)).toBe(150);
  });

  it("labels what there is room for, descending", () => {
    for (const [top, height] of [[100, 300], [100, 90], [500, 240]] as const) {
      const pressures = axisPressures(top, height);
      expect(pressures).toContain(top);
      expect([...pressures].sort((a, b) => b - a)).toEqual(pressures);
      const ys = pressures.map((pressure) => pressureY(pressure, top, height));
      for (let at = 1; at < ys.length; at += 1) expect(ys[at - 1]! - ys[at]!).toBeGreaterThanOrEqual(15);
    }
    expect(axisPressures(100, 300).length).toBeGreaterThan(axisPressures(100, 90).length);
    expect(axisPressures(500, 240).every((pressure) => pressure >= 500)).toBe(true);
  });

  it("spaces the barbs' levels, of the store's own", () => {
    const shown = barbLevels(LEVELS, 100, 290);
    expect(shown.every((level) => LEVELS.includes(level))).toBe(true);
    expect(shown).toEqual(expect.arrayContaining([850, 700, 500, 300, 200]));
    expect(shown).not.toContain(100);
    expect(barbLevels([1000, 850, 500], 500, 290)).toEqual([850, 1000]);
  });
});

describe("the ramps", () => {
  it("steps every fill over its range", () => {
    for (const fill of FILLS) {
      const bands = sectionBands(fill);
      const legend = sectionLegend(fill);
      expect(bands.rgb.length).toBe(legend.stops.length * 3);
      expect(legend.stops[0]!.from).toBe(legend.min);
      expect(legend.stops[legend.stops.length - 1]!.to).toBeCloseTo(legend.max, 9);
      expect(legend.ticks.every((tick) => tick >= legend.min && tick <= legend.max)).toBe(true);
      expect(new Set(legend.stops.map(({ color }) => color)).size).toBe(legend.stops.length);
    }
  });

  it("keeps the map's colour of 0 °C and spreads the wind's", () => {
    const legend = sectionLegend("tmp");
    // The band from 0 to 4 °C, its middle on the surface ramp.
    expect(legend.stops.find(({ from }) => from === 0)!.color).toBe("rgb(241, 235, 167)");
    expect(sectionLegend("cross").unit).toBe("wind");
    expect(sectionLegend("thetae").unit).toBe("kelvin");
  });
});

describe("a wind barb", () => {
  /** Where the lines of a barb end, from its point. */
  const ends = (side?: 1 | -1): [number, number][] => {
    const found: [number, number][] = [];
    const context = new Proxy({} as Record<string, unknown>, {
      get: (_, key) => (key === "lineTo" ? (x: number, y: number) => void found.push([x, y]) : () => {}),
      set: () => true,
    }) as unknown as CanvasRenderingContext2D;
    drawBarb(context, 0, 0, 270, 10, 1, side);
    return found;
  };

  it("carries its barbs on the side of the lower pressure, the southern hemisphere's the other", () => {
    const [shaft, barb] = ends();
    // A westerly: its shaft to the left, the barbs above it.
    expect(shaft![0]).toBeLessThan(0);
    expect(barb![1]).toBeLessThan(-1);
    const south = ends(-1);
    expect(south[0]).toEqual(shaft!.map((value) => expect.closeTo(value, 9)));
    expect(south[1]![1]).toBeCloseTo(-barb![1], 9);
    expect(south[1]![0]).toBeCloseTo(barb![0], 9);
  });
});

describe("paintFill", () => {
  const bands = { min: 0, step: 10, rgb: Uint8Array.from([1, 0, 0, 2, 0, 0, 3, 0, 0]) };
  const paint = (values: number[], points: number, width: number, height: number): number[] => {
    const pixels = new Uint32Array(width * height);
    paintFill(new DrawBuffers(), pixels, width, height, Float32Array.from(values), points, values.length / points, bands);
    return [...pixels].map((pixel) => pixel & 0xff);
  };

  it("cuts the bands from the interpolated value, the bottom row last", () => {
    // The grid's first row is the chart's lower edge.
    expect(paint([5, 5, 25, 25], 2, 2, 4)).toEqual([3, 3, 2, 2, 2, 2, 1, 1]);
  });

  it("ends in the end's colours and leaves what is missing clear", () => {
    expect(paint([-40, -40, -40, -40], 2, 2, 1)).toEqual([1, 1]);
    expect(paint([400, 400, 400, 400], 2, 2, 1)).toEqual([3, 3]);
    expect(paint([5, 5, Number.NaN, 5, 5, Number.NaN], 3, 4, 1)).toEqual([1, 1, 0, 0]);
  });
});

/** A 2D context that remembers what was called of it. */
function stubContext(calls: string[]): CanvasRenderingContext2D {
  const properties: Record<string | symbol, unknown> = {
    createImageData: (width: number, height: number) => ({ width, height, data: new Uint8ClampedArray(width * height * 4) }),
  };
  return new Proxy(properties, {
    get: (target, key) => target[key] ?? ((): void => void calls.push(String(key))),
    set: () => true,
  }) as unknown as CanvasRenderingContext2D;
}

const COLUMN: Record<string, number[]> = { tmp: [18, -18, -60], hgt: [110, 5650, 16300], orog: [30], prmsl: [1013], tmp2m: [20] };

function variable(value: number): BundleVariable {
  return {
    numericId: 1,
    id: "tmp",
    label: "",
    unit: "",
    quantization: { type: "linear", offset: value, scale: 0, minimumCode: 0, maximumCode: 254, nodataCode: 255 },
  } as unknown as BundleVariable;
}

const STORE: SectionStore = {
  model: "gfs",
  runTime: 0,
  grid: { lonLatToCell: () => null } as unknown as GridMap,
  width: 8,
  height: 8,
  blockHeight: 8,
  blockWidth: 8,
  arrays: new Map(
    Object.entries(COLUMN).map(([id, values]) => [
      id,
      { id, quantity: id as ColumnQuantity, levels: values.length > 1 ? [1000, 500, 100] : null, offsets: id === "orog" ? [0] : [0, 3600], members: values.map(variable) },
    ]),
  ),
};

const PLAN: SectionPlan = {
  line: { a: [0, 0], b: [2, 0] },
  length: 200_000,
  points: [0, 1, 2].map((lon) => ({ lon, lat: 0, along: lon * 1e5, bearing: 1 })),
  cells: new Uint32Array([0, 0, 0, 1, 0, 2]),
  corners: Uint32Array.from({ length: 12 }, (_, at) => at >> 2),
  weights: new Float32Array(12).fill(0.25),
  blocks: 1,
};

const STATE: SectionDrawState = {
  lead: 0,
  fill: "tmp",
  lines: { wind: true, zero: true, theta: false },
  barbs: true,
  top: 100,
  mapLevels: { fill: 850, lines: null },
  dark: false,
  units: { wind: { scale: 1, interval: 10, first: 20, heavy: 50 }, freezing: "0 °C", heightStep: 1000 },
};

/** Every array's cells, as the source reads them. */
function cells(): { array: string; indices: Uint32Array; codes: Uint8Array }[] {
  return Object.entries(COLUMN).map(([array, values]) => ({
    array,
    indices: new Uint32Array([0, 1, 2]),
    codes: new Uint8Array(3 * values.length * (array === "orog" ? 1 : 2)),
  }));
}

describe("SectionEngine", () => {
  function engine() {
    const replies: SectionReply[] = [];
    const calls: string[] = [];
    const frames: (() => void)[] = [];
    const context = stubContext(calls);
    const canvas = { width: 0, height: 0, getContext: () => context } as unknown as HTMLCanvasElement;
    const made = new SectionEngine(canvas, (reply) => replies.push(reply), (run) => frames.push(run));
    made.handle({ type: "resize", width: 60, height: 40, dpr: 2 });
    made.handle({ type: "section", epoch: 1, spec: fieldSpec(STORE, PLAN) });
    made.handle({ type: "draw", state: STATE, bands: sectionBands(STATE.fill) });
    const run = (): void => {
      for (const frame of frames.splice(0)) frame();
    };
    return { made, replies, calls, frames, canvas, run };
  }

  it("draws once a frame, whatever it was told in between", () => {
    const { made, frames, replies, canvas, run } = engine();
    made.handle({ type: "draw", state: { ...STATE, lead: 3600 }, bands: sectionBands("tmp") });
    expect(frames.length).toBe(1);
    run();
    expect(canvas.width).toBe(120);
    expect(canvas.height).toBe(80);
    expect(replies).toEqual([{ type: "drawn", epoch: 1, drawn: { complete: 0, noFrame: false, heights: [] } }]);
  });

  it("draws what has arrived, without the lines the store has nothing for", () => {
    vi.useFakeTimers();
    try {
      const { made, replies, calls, run } = engine();
      run();
      calls.length = 0;
      for (const part of cells()) made.handle({ type: "cells", ...part });
      vi.runAllTimers();
      run();
      expect(calls).toContain("putImageData");
      const last = replies[replies.length - 1]!;
      expect(last.type === "drawn" && last.drawn.complete).toBe(1);
      expect(last.type === "drawn" && last.drawn.heights.length).toBe(16);
    } finally {
      vi.useRealTimers();
    }
  });

  it("says where the store has no frame and leaves the plot clear", () => {
    const { made, replies, calls, run } = engine();
    for (const part of cells()) made.handle({ type: "cells", ...part });
    run();
    calls.length = 0;
    made.handle({ type: "draw", state: { ...STATE, lead: 7200 }, bands: sectionBands("tmp") });
    run();
    expect(calls).not.toContain("putImageData");
    const last = replies[replies.length - 1]!;
    expect(last.type === "drawn" && last.drawn.noFrame).toBe(true);
  });

  it("answers a probe of its own section only", () => {
    const { made, replies } = engine();
    for (const part of cells()) made.handle({ type: "cells", ...part });
    made.handle({ type: "probe", epoch: 1, id: 7, point: 1, pressure: 500 });
    made.handle({ type: "probe", epoch: 0, id: 8, point: 1, pressure: 500 });
    const readings = replies.filter((reply) => reply.type === "reading");
    expect(readings[0]!.reading!.temperature).toBeCloseTo(-18, 4);
    expect(readings[1]!.reading).toBeNull();
  });
});

describe("SectionRenderer", () => {
  it("draws on the page where a canvas cannot be handed over", async () => {
    const context = stubContext([]);
    const canvas = { width: 0, height: 0, getContext: () => context } as unknown as HTMLCanvasElement;
    const drawn = vi.fn();
    const renderer = new SectionRenderer(canvas, { onDrawn: drawn });
    renderer.setSection(STORE, PLAN);
    renderer.resize(60, 40, 1);
    renderer.draw(STATE);
    for (const { array, indices, codes } of cells()) renderer.addCells(array, indices, codes);
    const reading = await renderer.probe(1, 500);
    expect(reading!.temperature).toBeCloseTo(-18, 4);
    await vi.waitFor(() => expect(drawn).toHaveBeenCalled());
    renderer.destroy();
    expect(await renderer.probe(1, 500)).toBeNull();
  });

  it("ends on what throws here, as it would on a worker that died", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const canvas = { width: 0, height: 0, getContext: () => stubContext([]) } as unknown as HTMLCanvasElement;
    const failed = vi.fn();
    const renderer = new SectionRenderer(canvas, { onDrawn: () => {}, onFailed: failed });
    renderer.setSection(STORE, PLAN);
    await renderer.probe(1, 500);
    // Not the cells of any array of it.
    expect(() => renderer.addCells("tmp", new Uint32Array([0]), new Uint8Array(1))).not.toThrow();
    expect(failed).toHaveBeenCalledTimes(1);
    vi.restoreAllMocks();
  });

  it("lets a pending probe go when it is destroyed", async () => {
    const canvas = { width: 0, height: 0, getContext: () => null } as unknown as HTMLCanvasElement;
    const renderer = new SectionRenderer(canvas, { onDrawn: () => {} });
    const pending = renderer.probe(0, 500);
    renderer.destroy();
    expect(await pending).toBeNull();
  });
});
