import { beforeEach, describe, expect, it, vi } from "vitest";

import { createCrossSectionShell, type CrossSectionShellDeps } from "../../web/src/crosssection/shell";
import type { ForecastManifest } from "../../web/src/manifest";
import { parseView } from "../../web/src/viewstate";

/** The control the shell loads, as it was told things. */
const control = vi.hoisted(() => ({ calls: [] as unknown[][], made: 0, throws: false, seen: null as unknown }));

vi.mock("../../web/src/crosssection/source", () => ({ createSectionSource: (deps: unknown) => ({ of: deps }) }));
vi.mock("../../web/src/crosssection/control", () => ({
  createCrossSection: (deps: { manifest: unknown; source: unknown }) => {
    control.made += 1;
    control.seen = deps;
    return new Proxy({} as Record<string, unknown>, {
      get: (_, name) => {
        // Not a promise.
        if (name === "then") return undefined;
        return (...args: unknown[]) => {
          control.calls.push([name, ...args]);
          if (control.throws) throw new Error("broken");
          return name === "preview" ? { path: [], label: "whole", valid: true } : undefined;
        };
      },
    });
  },
}));

function setup(search = "", overrides: Partial<Pick<CrossSectionShellDeps, "dataBackend">> = {}) {
  const { view } = parseView(search, { field: "prate", lines: null, particles: true, setupLayers: { inflow: true, front: true } });
  const page = { manifest: { columns: {} } as ForecastManifest | null, inCase: false };
  const deps: CrossSectionShellDeps = {
    map: {} as CrossSectionShellDeps["map"],
    view,
    sourceDeps: {} as CrossSectionShellDeps["sourceDeps"],
    dataBackend: "zarr",
    capsule: document.createElement("div"),
    get manifest() {
      return page.manifest;
    },
    modelLabel: "GFS",
    get inCase() {
      return page.inCase;
    },
    playhead: () => 0,
    closeProbe: vi.fn(),
    syncUrl: vi.fn(),
    onTool: vi.fn(),
    ...overrides,
  };
  return { shell: createCrossSectionShell(deps), deps, view, page };
}

const LINE = "?section=39.90,116.40,35.70,139.70";
const loaded = () => vi.waitFor(() => expect(control.made).toBe(1));

beforeEach(() => {
  Object.assign(control, { calls: [], made: 0, throws: false, seen: null });
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

describe("the cross-section's shell", () => {
  it("is offered where the run has a store, and where a line is to be cleared", () => {
    const { shell, page, view } = setup();
    page.manifest = { bundles: [] } as never;
    shell.runOpened();
    expect(shell.offered).toBe(false);
    page.manifest = { borrowed: { bundles: [], columns: {} } } as never;
    shell.runOpened();
    expect(shell.offered).toBe(true);
    page.manifest = null;
    shell.runOpened();
    expect(shell.offered).toBe(false);
    view.section = { a: [100, 40], b: [130, 40] };
    expect(shell.offered).toBe(true);
    page.inCase = true;
    expect(shell.offered).toBe(false);
  });

  it("loads nothing for a reader who draws nothing", async () => {
    const { shell } = setup();
    shell.setInk(true);
    shell.runEnded();
    shell.runOpened();
    shell.frameShown();
    shell.probeShown(true);
    shell.probeShown(false);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(control.made).toBe(0);
  });

  it("loads the rest for a line a link brought, and tells it what it missed", async () => {
    const { shell, page } = setup(LINE);
    shell.setInk(true);
    shell.runEnded();
    shell.runOpened();
    await loaded();
    expect(control.calls).toEqual([["setInk", true], ["probeShown", false], ["runOpened"]]);
    // It reads the page as it is, not as it was.
    page.manifest = null;
    expect((control.seen as { manifest: unknown }).manifest).toBeNull();
    shell.frameShown();
    shell.runFailed();
    expect(control.calls.slice(3)).toEqual([["frameShown"], ["runFailed"]]);
  });

  it("loads the rest as the tool is taken up, and hands the line over", async () => {
    const { shell } = setup();
    shell.runOpened();
    expect(shell.preview([0, 0], [10, 0])).toEqual({ path: [[0, 0], [10, 0]], label: "", valid: true });
    expect(shell.preview([0, 0], null).valid).toBe(false);
    shell.armed();
    shell.drew([0, 0], [10, 0]);
    await loaded();
    await vi.waitFor(() => expect(control.calls).toContainEqual(["drew", [0, 0], [10, 0]]));
    expect(control.calls.map(([name]) => name)).toEqual(["setInk", "probeShown", "runOpened", "armed", "drew"]);
    expect(shell.preview([0, 0], [10, 0]).label).toBe("whole");
  });

  it("has no line in a case, nor on the container's backend", () => {
    const held = setup(LINE);
    held.page.inCase = true;
    held.shell.runEnded();
    expect(held.view.section).toBeNull();
    const other = setup(LINE, { dataBackend: "xue" });
    other.shell.runEnded();
    other.shell.runOpened();
    expect(other.view.section).toBeNull();
    expect(other.shell.offered).toBe(false);
  });

  it("never throws into the page", async () => {
    const { shell } = setup(LINE);
    shell.runOpened();
    await loaded();
    control.throws = true;
    expect(() => {
      shell.frameShown();
      shell.runEnded();
      shell.runOpened();
      shell.runFailed();
      shell.probeShown(true);
      shell.setInk(false);
    }).not.toThrow();
  });
});
