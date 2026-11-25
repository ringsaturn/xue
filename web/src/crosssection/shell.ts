/**
 * The vertical cross-section as the page knows it: whether the tool is
 * offered, and the rest handed on to `control.ts`, which is loaded with what
 * it draws with when there is first a line, or the tool is first taken up. A
 * reader who draws none loads none of it.
 *
 * Nothing that goes wrong in the section stops the page: a call from here
 * never throws into it.
 */

import type { LinePreview } from "../volumetool";
import type { DataBackend } from "../urlstate";
import type { CrossSection, CrossSectionDeps } from "./control";
import type { SectionSourceDeps } from "./source";
import type { LonLat } from "./types";

export interface CrossSectionShellDeps extends Omit<CrossSectionDeps, "source"> {
  readonly sourceDeps: SectionSourceDeps;
  readonly dataBackend: DataBackend;
  /** A case is on screen, or asked for: it has no section. */
  readonly inCase: boolean;
}

export function createCrossSectionShell(deps: CrossSectionShellDeps) {
  const { view } = deps;
  let control: CrossSection | null = null;
  let loading: Promise<CrossSection | null> | null = null;
  /** What has happened before the control was there to be told. */
  let run: "ended" | "opened" | "failed" = "ended";
  let hasStore = false;
  let darkGround = false;
  let probeShown = false;

  /** There is no section here at all: a case is of its own past, and the
   * container has no columns. */
  const excluded = (): boolean => deps.inCase || deps.dataBackend !== "zarr";

  function safely(call: () => void): void {
    try {
      call();
    } catch (error) {
      console.warn("cross-section:", error);
    }
  }

  function load(): Promise<CrossSection | null> {
    loading ??= Promise.all([import("./control"), import("./source")])
      .then(([{ createCrossSection }, { createSectionSource }]) => {
        // Not a spread: what the page's state is read through are getters.
        const made = createCrossSection(Object.create(deps, { source: { value: createSectionSource(deps.sourceDeps) } }) as CrossSectionDeps);
        control = made;
        safely(() => {
          made.setInk(darkGround);
          made.probeShown(probeShown);
          if (run === "opened") made.runOpened();
          else if (run === "failed") made.runFailed();
        });
        return made;
      })
      .catch((error: unknown) => {
        console.warn("cross-section: not loaded:", error instanceof Error ? error.message : error);
        loading = null;
        return null;
      });
    return loading;
  }

  return {
    /** Whether the view tile offers the tool: the run has a store, or a line
     * is set, whose way out the button is. */
    get offered(): boolean {
      return !excluded() && (hasStore || view.section !== null);
    },
    /** Whether a line is set. */
    get active(): boolean {
      return view.section !== null;
    },
    clear(): void {
      if (control) safely(control.clear);
      else {
        view.section = null;
        deps.syncUrl();
        deps.onTool();
      }
    },
    /** What a drag from `a` to `b` would draw; the ends alone in the moment
     * before the control is there. */
    preview(a: LonLat | null, b: LonLat | null): LinePreview {
      return control?.preview(a, b) ?? { path: a && b ? [a, b] : [], label: "", valid: a !== null && b !== null };
    },
    /** The tool has been taken up. */
    armed(): void {
      void load().then((made) => safely(() => made?.armed()));
    },
    drew(a: LonLat, b: LonLat): void {
      void load().then((made) => safely(() => made?.drew(a, b)));
    },
    /** The run on screen is over. */
    runEnded(): void {
      run = "ended";
      if (excluded()) view.section = null;
      safely(() => control?.runEnded());
    },
    /** A dataset is open, or the same run's store has changed. */
    runOpened(): void {
      run = "opened";
      hasStore = (deps.manifest?.columns ?? deps.manifest?.borrowed?.columns) !== undefined;
      deps.onTool();
      if (control) safely(control.runOpened);
      else if (view.section) void load();
    },
    /** The run did not open. */
    runFailed(): void {
      run = "failed";
      safely(() => control?.runFailed());
    },
    frameShown(): void {
      safely(() => control?.frameShown());
    },
    probeShown(shown: boolean): void {
      probeShown = shown;
      safely(() => control?.probeShown(shown));
    },
    setInk(dark: boolean): void {
      darkGround = dark;
      safely(() => control?.setInk(dark));
    },
  };
}
