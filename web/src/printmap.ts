import type { Map as MaplibreMap } from "maplibre-gl";
import { enterPaper } from "./theme";

/** What the printed sheet reads off the page as the print starts. */
export interface PrintSheetDeps {
  readonly map: MaplibreMap;
  /** The credit line the data and the basemap ask for. */
  credit(): string;
  /** Hold the playhead, so the chart is one valid time. */
  pause(): void;
}

/** How long the paper basemap gets to load before the print goes ahead
 * with what has arrived. */
const SETTLE_TIMEOUT_MS = 15_000;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

/** The paper basemap's tiles and sources in. Polled rather than awaited on
 * `idle`, which a continuously repainting layer never lets fire. */
async function mapSettled(map: MaplibreMap): Promise<void> {
  const deadline = performance.now() + SETTLE_TIMEOUT_MS;
  await sleep(100);
  while (!(map.loaded() && map.areTilesLoaded()) && performance.now() < deadline) await sleep(100);
}

/** The paper ground's undo while a chart is prepared or printed; null on
 * screen. */
let paperRestore: (() => void) | null = null;
/** A prepare under way. */
let preparing: Promise<void> | null = null;
/** Set once the sheet is filled and the still decoded: a print may start. */
let ready = false;

/** Put the screen back: the theme, the empty sheet. */
export function leavePaper(): void {
  ready = false;
  const restore = paperRestore;
  paperRestore = null;
  document.getElementById("print-map")?.removeAttribute("src");
  document.getElementById("print-legend")?.replaceChildren();
  restore?.();
}

/** Whether a prepared chart is waiting for the print. */
export function chartReady(): boolean {
  return ready;
}

/** Whether `window.print()` called now still counts as the reader's tap.
 * Phone browsers drop a print that does not (iOS Safari silently), and the
 * prepare outlasts the few seconds a tap is credited for. Without the API,
 * a touch screen is taken to have lost it and a desktop not. */
export function printAllowedNow(): boolean {
  const activation = (navigator as Navigator & { userActivation?: { isActive: boolean } }).userActivation;
  if (activation) return activation.isActive;
  return !window.matchMedia("(pointer: coarse)").matches;
}

/** Move the map to its paper ground, wait for it, take the still and fill
 * the sheet. The ground stays until leavePaper, which the print's close or
 * a cancel calls. */
export function prepareChart(deps: PrintSheetDeps): Promise<void> {
  if (ready) return Promise.resolve();
  if (preparing) return preparing;
  deps.pause();
  if (!paperRestore) paperRestore = enterPaper();
  preparing = (async () => {
    try {
      await mapSettled(deps.map);
      const image = document.getElementById("print-map") as HTMLImageElement;
      await setStill(image, await captureMap(deps.map));
      fillSheet(deps);
      ready = paperRestore !== null;
    } catch (error) {
      leavePaper();
      throw error;
    } finally {
      preparing = null;
    }
  })();
  return preparing;
}

/** Open the print dialog on the prepared chart; call it in a tap's own task
 * on a phone. The screen comes back once the dialog closes: afterprint,
 * where a browser blocks in print() or fires it; otherwise the reader's
 * next touch on the page, which only comes once the dialog is gone. */
export function printPrepared(): void {
  if (!ready) return;
  let back = false;
  const done = () => {
    if (back) return;
    back = true;
    window.removeEventListener("afterprint", done);
    window.removeEventListener("pointerdown", done, true);
    leavePaper();
  };
  window.addEventListener("afterprint", done, { once: true });
  try {
    window.print();
  } catch {
    done();
    return;
  }
  if (!back) window.addEventListener("pointerdown", done, { once: true, capture: true });
}

/** Prepare the chart and print it straight away: ⌘P, where a desktop
 * browser takes a print outside a tap. */
export async function printChart(deps: PrintSheetDeps): Promise<void> {
  try {
    await prepareChart(deps);
  } catch {
    return;
  }
  printPrepared();
}

function text(id: string): string {
  return (document.getElementById(id)?.textContent ?? "").replace(/\s+/g, " ").trim();
}

function shown(element: HTMLElement | null): element is HTMLElement {
  return element !== null && !element.hidden && getComputedStyle(element).display !== "none";
}

/** The still of the map: read in the map's `render` event, the one task in
 * which the WebGL drawing buffer still holds the frame. */
function captureMap(map: MaplibreMap): Promise<string> {
  return new Promise((resolve) => {
    map.once("render", () => {
      try {
        resolve(map.getCanvas().toDataURL("image/png"));
      } catch {
        resolve("");
      }
    });
    map.triggerRepaint();
  });
}

/** Fill the sheet's heading, legend and credits from the page. */
function fillSheet(deps: PrintSheetDeps): void {
  const caseBanner = document.getElementById("case-banner");
  const caseTitle = shown(caseBanner) ? text("case-title") : "";
  document.getElementById("print-title")!.textContent = [caseTitle, text("variable-title")].filter(Boolean).join(" · ");
  document.getElementById("print-code")!.textContent = text("variable-code");
  const valid = text("valid-time");
  const lead = text("forecast-hour");
  document.getElementById("print-stamp")!.textContent = [
    text("model-eyebrow"),
    `${text("run-time-label")} ${text("run-time")}`,
    valid && valid !== "--" ? `${text("valid-time-label")} ${valid}${lead ? ` (${lead})` : ""}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  document.getElementById("print-credit")!.textContent = deps.credit();

  const legendSlot = document.getElementById("print-legend")!;
  const legend = document.getElementById("legend");
  legendSlot.replaceChildren();
  if (shown(legend)) {
    const copy = legend.cloneNode(true) as HTMLElement;
    for (const node of [copy, ...copy.querySelectorAll<HTMLElement>("[id]")]) node.removeAttribute("id");
    legendSlot.append(copy);
  }
}

/** Set the still and wait until it is decoded: the print lays the page out
 * as it stands, and an image still decoding there has no size. */
async function setStill(image: HTMLImageElement, url: string): Promise<void> {
  if (!url) {
    image.removeAttribute("src");
    return;
  }
  image.src = url;
  try {
    await image.decode();
  } catch {
    // Printed without the still.
  }
}

/** Print the map viewer as a chart: the controls give way to a heading, a
 * still of the map, its legend and the credits (see the print rules in
 * style.css). The still is taken because a WebGL canvas prints blank in
 * some browsers. The browser's own print command fills the sheet in
 * beforeprint, where the still may not decode in time; ⌘P and the export
 * sheet's button prepare it first (prepareChart). */
export function printMapSheet(deps: PrintSheetDeps): void {
  const image = document.getElementById("print-map") as HTMLImageElement | null;
  if (!image) return;

  window.addEventListener("beforeprint", () => {
    if (ready || preparing) return;
    try {
      deps.map.redraw();
      image.src = deps.map.getCanvas().toDataURL("image/png");
    } catch {
      image.removeAttribute("src");
    }
    fillSheet(deps);
  });

  window.addEventListener("afterprint", () => {
    if (paperRestore) return;
    image.removeAttribute("src");
    document.getElementById("print-legend")?.replaceChildren();
  });

  // ⌘P / Ctrl+P prints the chart on paper rather than the screen as it is.
  window.addEventListener("keydown", (event) => {
    if (event.key.toLowerCase() !== "p" || !(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) return;
    event.preventDefault();
    void printChart(deps);
  });
}
