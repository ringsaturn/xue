// The export sheet: both loop exports start here. The capture and the
// encoders are gifexport.ts and mp4export.ts; this is the controller.
import type { Map as MaplibreMap } from "maplibre-gl";
import { captionText } from "./casecaptions";
import { formatBytes } from "./format";
import {
  deliverGif,
  exportGif,
  GIF_MAX_FRAMES,
  gifFileName,
  gifFrameWindow,
  type GifHost,
  type GifProgress,
  gifSize,
} from "./gifexport";
import { locale, onLocaleChange, t } from "./i18n";
import type { BundleVariable } from "./manifest";
import {
  deliverMp4,
  exportMp4,
  isMp4ExportSupported,
  MP4_DEFAULT_SIZE,
  MP4_FPS,
  MP4_MAX_FRAMES,
  mp4Bitrate,
  mp4FileName,
  mp4Size,
  mp4SizeChoices,
  type Mp4SizeId,
  mp4SizeRung,
  parseStoredMp4Size,
} from "./mp4export";
import type { WindParticleLayer } from "./particles";
import { FPS_LADDER, parseStoredFps, type PlaybackFps } from "./playback";
import { createSheet } from "./sheet";
import type { ShowcaseCase } from "./showcase-catalog";

/** What the export sheet reads off the page: its own elements, the
 * playhead and the axis (live, through getters), and the transport it
 * drives while a loop is cut. */
export interface ExportSheetDeps {
  readonly playbackFps: PlaybackFps;
  /** The localStorage keys the choices are kept under. */
  readonly MP4_SIZE_KEY: string;
  readonly EXPORT_FORMAT_KEY: string;
  readonly EXPORT_FPS_KEY: string;
  readonly map: MaplibreMap;
  readonly slider: HTMLInputElement;
  readonly exportTrigger: HTMLButtonElement;
  readonly exportSheet: HTMLElement;
  readonly exportFormatGroup: HTMLElement;
  readonly exportSizeGroup: HTMLElement;
  readonly exportFpsGroup: HTMLElement;
  readonly exportStart: HTMLInputElement;
  readonly exportEnd: HTMLInputElement;
  readonly exportStartValue: HTMLOutputElement;
  readonly exportEndValue: HTMLOutputElement;
  readonly exportSummary: HTMLElement;
  readonly exportProgress: HTMLElement;
  readonly exportRun: HTMLButtonElement;
  readonly exportRing: SVGCircleElement;
  readonly variableCode: HTMLElement;
  readonly variableTitle: HTMLElement;
  readonly windLayer: WindParticleLayer | null;
  readonly activeCase: ShowcaseCase | null;
  readonly activeVariable: BundleVariable | null;
  readonly activeFrameIndex: number | null;
  readonly playing: boolean;
  readonly formatDate: (value: string | number) => string;
  readonly frameCount: () => number;
  readonly isStaticAxis: () => boolean;
  readonly frameHoldMs: (index: number) => number;
  readonly showingObservations: () => boolean;
  readonly showingDailyRecord: () => boolean;
  readonly formatUtcDay: (validTime: number) => string;
  readonly frameValidTime: (index: number) => number;
  readonly formatLead: (index: number) => string;
  readonly stopPlayback: () => void;
  readonly blendFrames: (index: number, next: number, weight: number) => boolean;
  readonly startPlayback: () => void;
  readonly trySelectFrame: (index: number) => boolean;
}

/** The export sheet's controller: format, size, rate and span choices,
 * the size estimate, and the GIF / MP4 loop export it runs. */
export function createExportSheet(deps: ExportSheetDeps) {
  const {
    MP4_SIZE_KEY,
    EXPORT_FORMAT_KEY,
    EXPORT_FPS_KEY,
    map,
    slider,
    exportTrigger,
    exportSheet,
    exportFormatGroup,
    exportSizeGroup,
    exportFpsGroup,
    exportStart,
    exportEnd,
    exportStartValue,
    exportEndValue,
    exportSummary,
    exportProgress,
    exportRun,
    exportRing,
    variableCode,
    variableTitle,
    formatDate,
    frameCount,
    isStaticAxis,
    frameHoldMs,
    showingObservations,
    showingDailyRecord,
    formatUtcDay,
    frameValidTime,
    formatLead,
    stopPlayback,
    blendFrames,
    startPlayback,
    trySelectFrame,
  } = deps;
  /** The loop export under way, GIF or MP4, aborted by a second press or by a
   * new dataset. One at a time: an export drives the playhead. */
  let loopExport: AbortController | null = null;

  const gifHost: GifHost = {
    map,
    show: (index) => trySelectFrame(index),
    blend: (index, weight) => index + 1 < frameCount() && blendFrames(index, index + 1, weight),
    caption: (index) => ({
      title: variableTitle.textContent ?? "",
      code: variableCode.textContent ?? "",
      // The file travels without the capsule that names the zone, so the
      // stamp carries the offset itself.
      stamp: showingDailyRecord()
        ? `${formatUtcDay(frameValidTime(index))} UTC`
        : showingObservations()
          ? formatDate(frameValidTime(index))
          : `${formatLead(index)} · ${formatDate(frameValidTime(index))}`,
      subtitle: deps.activeCase?.captions ? captionText(deps.activeCase.captions, frameValidTime(index), locale) : "",
    }),
    credit: gifCredit,
    holdMs: exportHoldMs,
    mp4Size: () => mp4SizeChoice,
    holdMotion: (stepSeconds) => deps.windLayer?.setFixedStep(stepSeconds),
    stepMotion: () => deps.windLayer?.armStep(),
  };

  type ExportFormat = "gif" | "mp4";

  /** What the sheet is set to; format, size and rate are kept across visits,
   * so saving again is a press on the corner button and one on EXPORT. */
  let exportFormat: ExportFormat = "gif";
  let mp4SizeChoice: Mp4SizeId = MP4_DEFAULT_SIZE;
  /** The file's rate, null while it follows the playback rate. */
  let exportFpsChoice: PlaybackFps | null = null;
  let mp4Supported = false;
  /** The span of frames, kept while the time axis keeps its length; a new
   * axis starts again from the frame on screen. */
  let exportRange: { start: number; end: number; count: number } | null = null;
  /** What the running export last reported, for the sheet opened mid-way. */
  let exportStatus = "";
  /** Set when the last export failed, until the next one or a new dataset. */
  let exportFailed = false;

  /** GIF bytes per pixel per frame, a rough mean of map loops through the
   * 256-colour encoder (a dry, flat field comes in under it, a busy radar
   * loop over); the sheet's figure is labelled as an estimate. */
  const GIF_BYTES_PER_PIXEL = 0.35;

  function exportFps(): PlaybackFps {
    return exportFpsChoice ?? deps.playbackFps;
  }

  /** A frame's hold in the file: its playback hold at the rate chosen for the
   * file rather than the rate playing. */
  function exportHoldMs(index: number): number {
    return (frameHoldMs(index) * deps.playbackFps) / exportFps();
  }

  function remember(key: string, value: string): void {
    try {
      localStorage.setItem(key, value);
    } catch {
      // Preference just won't persist.
    }
  }

  /** The notices the credit line shows for the data on screen, then the
   * basemap's, after the site's address. Which notices are on is the
   * stylesheet's call (`body[data-model]`, `body[data-credits]`), and a
   * notice's own display reads it even where the line itself is hidden. */
  function gifCredit(): string {
    const notices = [...document.querySelectorAll<HTMLElement>(".source-credit")]
      .filter((credit) => getComputedStyle(credit).display !== "none")
      .map((credit) => (credit.textContent ?? "").replace(/\s+/g, " ").replace(/\s*;\s*$/, "").trim());
    return [window.location.host, ...notices, "© OpenStreetMap, Protomaps"].join("; ");
  }

  /** One of the two files the loop is saved as. */
  interface LoopExportKind {
    /** What the format reads in the sheet, an instrument word in every locale. */
    idle: string;
    /** Frames of data in one file at most. */
    maxFrames: number;
    /** The share of the progress the capture takes; encoding takes the rest. */
    captureShare: number;
    run: (host: GifHost, frames: number[], onProgress: (progress: GifProgress) => void, signal: AbortSignal) => Promise<Blob>;
    fileName: (code: string, validTime: number) => string;
    deliver: (blob: Blob, name: string) => Promise<void>;
  }

  const LOOP_EXPORTS: Record<ExportFormat, LoopExportKind> = {
    gif: {
      idle: "GIF",
      maxFrames: GIF_MAX_FRAMES,
      // The GIF's palette and LZW pass happen after the capture, in a worker.
      captureShare: 0.8,
      run: exportGif,
      fileName: gifFileName,
      deliver: deliverGif,
    },
    mp4: {
      idle: "MP4",
      maxFrames: MP4_MAX_FRAMES,
      // The MP4 is encoded as it is captured; the flush at the end is short.
      captureShare: 0.95,
      run: exportMp4,
      fileName: mp4FileName,
      deliver: deliverMp4,
    },
  };

  /** The frames the file will hold: the span set in the sheet, or the
   * stretch from the frame on screen onward, cut to the format's ceiling. */
  function exportFrames(): number[] {
    const count = frameCount();
    const max = LOOP_EXPORTS[exportFormat].maxFrames;
    if (!exportRange || exportRange.count !== count) {
      const span = gifFrameWindow(count, deps.activeFrameIndex ?? Number(slider.value), max);
      exportRange = { start: span[0] ?? 0, end: span[span.length - 1] ?? 0, count };
    }
    const start = Math.min(Math.max(0, exportRange.start), count - 1);
    const end = Math.min(Math.max(start, exportRange.end), count - 1, start + max - 1);
    exportRange = { start, end, count };
    return Array.from({ length: end - start + 1 }, (_, offset) => start + offset);
  }

  /** The pixel size of the file for the canvas as it is now. */
  function exportPixels(): { width: number; height: number } {
    const canvas = map.getCanvas();
    if (exportFormat === "gif") return gifSize(canvas.width, canvas.height);
    const rung = mp4SizeRung(mp4SizeChoice, canvas.width, canvas.height);
    return mp4Size(canvas.width, canvas.height, rung.width, rung.height);
  }

  function frameReadout(index: number): string {
    if (showingDailyRecord()) return `${formatUtcDay(frameValidTime(index))} UTC`;
    return showingObservations()
      ? formatDate(frameValidTime(index))
      : `${formatLead(index)} · ${formatDate(frameValidTime(index))}`;
  }

  /** A row of mutually exclusive choices; the pressed one is checked. */
  function renderChoices<Value extends string | number>(
    group: HTMLElement,
    options: readonly { value: Value; label: string }[],
    current: Value,
    pick: (value: Value) => void,
  ): void {
    group.replaceChildren(
      ...options.map(({ value, label }) => {
        const button = document.createElement("button");
        button.type = "button";
        button.setAttribute("role", "radio");
        button.setAttribute("aria-checked", String(value === current));
        button.textContent = label;
        button.disabled = loopExport !== null;
        button.addEventListener("click", () => {
          pick(value);
          renderExportSheet();
        });
        return button;
      }),
    );
  }

  /** Write the sheet from the state: the choices, the span, what the file
   * comes to, and the button's reading. */
  function renderExportSheet(): void {
    const running = loopExport !== null;
    const formats: { value: ExportFormat; label: string }[] = [{ value: "gif", label: "GIF" }];
    if (mp4Supported) formats.push({ value: "mp4", label: "MP4" });
    renderChoices(exportFormatGroup, formats, exportFormat, (value) => {
      exportFormat = value;
      remember(EXPORT_FORMAT_KEY, value);
    });
    const canvas = map.getCanvas();
    const pixels = exportPixels();
    if (exportFormat === "mp4") {
      const choices = mp4SizeChoices(canvas.width, canvas.height);
      const current = mp4SizeRung(mp4SizeChoice, canvas.width, canvas.height).id;
      renderChoices(exportSizeGroup, choices.map((rung) => ({ value: rung.id, label: rung.label })), current, (value) => {
        mp4SizeChoice = value;
        remember(MP4_SIZE_KEY, value);
      });
    } else {
      // The GIF has one size, the canvas cut to what a chat takes.
      renderChoices(exportSizeGroup, [{ value: "gif", label: `${pixels.width}×${pixels.height}` }], "gif", () => {});
    }
    renderChoices(exportFpsGroup, FPS_LADDER.map((fps) => ({ value: fps, label: `${fps} FPS` })), exportFps(), (value) => {
      exportFpsChoice = value;
      remember(EXPORT_FPS_KEY, String(value));
    });

    const frames = exportFrames();
    const last = Math.max(0, frameCount() - 1);
    for (const input of [exportStart, exportEnd]) {
      input.max = String(last);
      input.disabled = running || last === 0;
    }
    exportStart.value = String(frames[0] ?? 0);
    exportEnd.value = String(frames[frames.length - 1] ?? 0);
    exportStartValue.value = frameReadout(frames[0] ?? 0);
    exportEndValue.value = frameReadout(frames[frames.length - 1] ?? 0);

    exportProgress.hidden = !running;
    exportRun.textContent = t(running ? "exportCancel" : "exportRun");
    exportRun.classList.toggle("is-busy", running);
    exportRun.disabled = !running && (!deps.activeVariable || isStaticAxis());
    if (running) {
      exportSummary.textContent = exportStatus;
      return;
    }
    const seconds = frames.reduce((sum, index) => sum + exportHoldMs(index), 0) / 1000;
    const bytes = exportFormat === "mp4"
      ? (mp4Bitrate(pixels.width, pixels.height, MP4_FPS) * seconds) / 8
      : pixels.width * pixels.height * frames.length * GIF_BYTES_PER_PIXEL;
    const summary = t("exportSummary", {
      size: `${pixels.width}×${pixels.height}`,
      frames: frames.length,
      seconds: seconds.toFixed(seconds < 10 ? 1 : 0),
      bytes: formatBytes(bytes),
    });
    exportSummary.textContent = exportFailed ? `${t("exportFailed")} · ${summary}` : summary;
  }

  /** Move one end of the span; the other follows when the span would turn
   * over or outgrow the format's ceiling. */
  function setExportBound(which: "start" | "end", value: number): void {
    const frames = exportFrames();
    const max = LOOP_EXPORTS[exportFormat].maxFrames;
    let start = frames[0] ?? 0;
    let end = frames[frames.length - 1] ?? 0;
    if (which === "start") {
      start = value;
      end = Math.min(Math.max(end, start), start + max - 1);
    } else {
      end = value;
      start = Math.max(Math.min(start, end), end - max + 1);
    }
    exportRange = { start, end, count: frameCount() };
    renderExportSheet();
  }

  /** The progress on the corner button's ring and in the open sheet. */
  function showExportProgress(kind: LoopExportKind, progress: GifProgress): void {
    const share = progress.phase === "capture" ? kind.captureShare : 1 - kind.captureShare;
    const base = progress.phase === "capture" ? 0 : kind.captureShare;
    const fraction = base + share * (progress.total > 0 ? progress.done / progress.total : 0);
    const percentDone = Math.round(fraction * 100);
    exportRing.style.strokeDasharray = `${percentDone} 100`;
    exportProgress.style.setProperty("--export-progress", String(percentDone));
    exportStatus = progress.phase === "capture"
      ? t("exportCapturing", { done: progress.done, total: progress.total })
      : t("exportEncoding", { percent: percentDone });
    if (!exportSheet.hidden) exportSummary.textContent = exportStatus;
  }

  function setExportRunning(running: boolean): void {
    exportTrigger.classList.toggle("is-busy", running);
    exportRing.style.strokeDasharray = "0 100";
    exportProgress.style.setProperty("--export-progress", "0");
    // A running export stays reachable to cancel even once the axis is gone.
    if (running) exportTrigger.disabled = false;
    if (!exportSheet.hidden) renderExportSheet();
  }

  /** Whether the loop can be saved now: a time axis, and no export running. */
  function setLoopExportEnabled(enabled: boolean): void {
    exportTrigger.disabled = !enabled && !loopExport;
    if (!enabled) exportFailed = false;
    if (!exportSheet.hidden) renderExportSheet();
  }

  /** Capture the loop as the sheet is set and hand the file over, then put
   * the frame and the playback back as they were; a press while it runs
   * cancels it. */
  async function saveLoop(): Promise<void> {
    if (loopExport) {
      loopExport.abort();
      return;
    }
    if (!deps.activeVariable || isStaticAxis()) return;
    const kind = LOOP_EXPORTS[exportFormat];
    const controller = new AbortController();
    loopExport = controller;
    exportFailed = false;
    exportStatus = "";
    const resumeVariable = deps.activeVariable;
    const resumeIndex = deps.activeFrameIndex ?? Number(slider.value);
    const resumePlaying = deps.playing;
    stopPlayback();
    const frames = exportFrames();
    setExportRunning(true);
    const name = kind.fileName(variableCode.textContent ?? "", frameValidTime(frames[0]!));
    try {
      const blob = await kind.run(gifHost, frames, (progress) => showExportProgress(kind, progress), controller.signal);
      await kind.deliver(blob, name);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        console.error(`${kind.idle} export failed`, error);
        exportFailed = true;
      }
    } finally {
      loopExport = null;
      setExportRunning(false);
      exportTrigger.classList.toggle("is-failed", exportFailed);
      if (exportFailed) {
        setTimeout(() => exportTrigger.classList.remove("is-failed"), 3000);
      }
      // A new dataset aborts the export and owns the frame from then on.
      if (deps.activeVariable === resumeVariable) {
        trySelectFrame(resumeIndex);
        if (resumePlaying) startPlayback();
      } else {
        exportTrigger.disabled = isStaticAxis();
      }
    }
  }

  /** The trigger, the sheet's controls and the stored choices, wired where
   * the page's other transport controls are. */
  function attachExportSheet(): void {
    // A tap opens the sheet; a long press saves again as the sheet was last
    // set, and the tap that ends it must not open the sheet as well.
    let exportLongPress = 0;
    let exportLongPressed = false;
    const EXPORT_LONG_PRESS_MS = 550;
    createSheet({
      trigger: exportTrigger,
      sheet: exportSheet,
      canOpen: () => {
        if (!exportLongPressed) return true;
        exportLongPressed = false;
        return false;
      },
      initialFocus: () => exportRun,
    });
    exportTrigger.addEventListener("click", () => {
      if (!exportSheet.hidden) renderExportSheet();
    });
    exportTrigger.addEventListener("pointerdown", (event) => {
      exportLongPressed = false;
      if (event.button !== 0 || loopExport) return;
      window.clearTimeout(exportLongPress);
      exportLongPress = window.setTimeout(() => {
        exportLongPressed = true;
        navigator.vibrate?.(12);
        void saveLoop();
      }, EXPORT_LONG_PRESS_MS);
    });
    for (const type of ["pointerup", "pointerleave", "pointercancel"] as const) {
      exportTrigger.addEventListener(type, () => window.clearTimeout(exportLongPress));
    }
    // The press is held on purpose; no callout or menu over it.
    exportTrigger.addEventListener("contextmenu", (event) => event.preventDefault());
    exportRun.addEventListener("click", () => void saveLoop());
    exportStart.addEventListener("input", () => setExportBound("start", Number(exportStart.value)));
    exportEnd.addEventListener("input", () => setExportBound("end", Number(exportEnd.value)));
    // MP4 is offered only where the browser can encode H.264 itself.
    void isMp4ExportSupported().then((supported) => {
      mp4Supported = supported;
      if (!supported && exportFormat === "mp4") exportFormat = "gif";
      if (!exportSheet.hidden) renderExportSheet();
    });
    // The choices stick across visits like the playback rate; the size the
    // canvas cannot fill falls to the largest it can when the file is cut.
    try {
      mp4SizeChoice = parseStoredMp4Size(localStorage.getItem(MP4_SIZE_KEY)) ?? MP4_DEFAULT_SIZE;
      const format = localStorage.getItem(EXPORT_FORMAT_KEY);
      if (format === "gif" || format === "mp4") exportFormat = format;
      exportFpsChoice = parseStoredFps(localStorage.getItem(EXPORT_FPS_KEY));
    } catch {
      // Defaults stand.
    }
    window.addEventListener("resize", () => {
      if (!exportSheet.hidden) renderExportSheet();
    });
    onLocaleChange(() => {
      if (!exportSheet.hidden) renderExportSheet();
    });
  }

  return {
    setLoopExportEnabled,
    attachExportSheet,
    get loopExport() {
      return loopExport;
    },
  };
}
