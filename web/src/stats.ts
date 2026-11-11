// The "stats for nerds" card, the debug copy and the map's context menu.
import type { Map as MaplibreMap } from "maplibre-gl";
import { formatBytes } from "./format";
import { t } from "./i18n";
import type { BundleMetadata, ForecastModelId } from "./manifest";
import type { PlaybackFps } from "./playback";
import type { ProbePanel } from "./probepanel";
import type { VariableSession } from "./session";
import { tileCount } from "./tiles";
import { displayZone, zonePref } from "./timezone";

/** What the stats card and the debug copy read off the page: the card's
 * own elements, the run and the session on screen (live, through
 * getters), and the decode and cache counters. */
export interface StatsDeps {
  readonly playbackFps: PlaybackFps;
  readonly neededGridWidth: () => number;
  readonly planeCacheBudgetBytes: () => number;
  readonly prefetchWindowFrames: () => number;
  readonly map: MaplibreMap;
  readonly loadStatus: HTMLElement;
  readonly contextMenu: HTMLElement;
  readonly statsMenuToggle: HTMLButtonElement;
  readonly statDataset: HTMLElement;
  readonly statGrid: HTMLElement;
  readonly statCacheBytes: HTMLElement;
  readonly statDecode: HTMLElement;
  readonly statDecodeRate: HTMLElement;
  readonly statGraph: HTMLCanvasElement;
  readonly statViewport: HTMLElement;
  readonly statConnection: HTMLElement;
  readonly currentRun: string | null;
  readonly metadata: BundleMetadata | null;
  readonly overlaySlots: () => { readonly session: VariableSession | null }[];
  readonly selectedModelId: ForecastModelId;
  readonly activeSession: VariableSession | null;
  readonly activeFrameIndex: number | null;
  readonly planeCacheBytes: number;
  readonly lastDecodeMs: number | null;
  readonly DECODE_GRAPH_WINDOW_MS: number;
  readonly DECODE_GRAPH_BUCKET_MS: number;
  readonly decodeEvents: { at: number; bytes: number; ms: number; }[];
  readonly decodeRateBytesPerSec: () => number;
  readonly reducedMotion: MediaQueryList;
  readonly frameCount: () => number;
  readonly formatLead: (index: number) => string;
  readonly cachedFrameCount: () => number;
  readonly probePanelControl: Pick<ProbePanel, "probeZone">;
  readonly formatReadout: (session: Pick<VariableSession, "format" | "tier">) => string;
}

/** The stats card's controller: its rows and decode graph, the debug
 * info the map's context menu copies, and the menu itself. */
export function createStats(deps: StatsDeps) {
  const {
    neededGridWidth,
    planeCacheBudgetBytes,
    prefetchWindowFrames,
    map,
    loadStatus,
    contextMenu,
    statsMenuToggle,
    statDataset,
    statGrid,
    statCacheBytes,
    statDecode,
    statDecodeRate,
    statGraph,
    statViewport,
    statConnection,
    overlaySlots,
    DECODE_GRAPH_WINDOW_MS,
    DECODE_GRAPH_BUCKET_MS,
    decodeEvents,
    decodeRateBytesPerSec,
    reducedMotion,
    frameCount,
    formatLead,
    cachedFrameCount,
    formatReadout,
  } = deps;
  // The data card is a YouTube-style "stats for nerds" panel: hidden by
  // default, pinned by the map's right-click menu (body.stats-visible shows the
  // extended rows below), and closable from the card itself.
  const STATS_VISIBLE_KEY = "g2pv-stats-visible";

  function statsVisible(): boolean {
    return document.body.classList.contains("stats-visible");
  }

  function setStatsVisible(visible: boolean): void {
    document.body.classList.toggle("stats-visible", visible);
    statsMenuToggle.setAttribute("aria-checked", String(visible));
    if (visible) {
      updateStatsReadout();
      startStatsGraph();
    }
    try {
      localStorage.setItem(STATS_VISIBLE_KEY, visible ? "1" : "0");
    } catch {
      // Preference just won't persist.
    }
  }

  /** How much of the grid the view is fetching, when it is fetching a subset —
   * empty on the whole-plane path, which is every non-streaming session and
   * every global view. */
  function tileShare(): string {
    const geometry = deps.activeSession?.tiles;
    const tiles = deps.activeSession?.viewTiles;
    if (!geometry || !tiles) return "";
    return ` · ${tileCount(tiles)} / ${geometry.columns * geometry.rows} tiles`;
  }

  function connectionLabel(): string {
    const connection = (navigator as { connection?: { saveData?: boolean; effectiveType?: string } }).connection;
    if (!connection?.effectiveType) return "--";
    return connection.saveData ? `${connection.effectiveType} · ${t("saveData")}` : connection.effectiveType;
  }

  function updateStatsReadout(): void {
    if (!statsVisible()) return;
    statDataset.textContent = deps.currentRun ? `${deps.selectedModelId}.${deps.currentRun}` : "--";
    const grid = deps.activeSession?.metadata.grid ?? null;
    statGrid.textContent = grid ? `${grid.width} × ${grid.height}` : "--";
    statCacheBytes.textContent = `${formatBytes(deps.planeCacheBytes)} / ${formatBytes(planeCacheBudgetBytes())}`;
    statDecode.textContent = deps.lastDecodeMs === null ? "--" : `${deps.lastDecodeMs.toFixed(1)} ms`;
    statDecodeRate.textContent = `${formatBytes(decodeRateBytesPerSec())}/s`;
    const needed = Math.round(neededGridWidth());
    const columns = grid ? `${needed} / ${grid.width} col` : `${needed} col`;
    statViewport.textContent = `${columns}${tileShare()}`;
    statConnection.textContent = connectionLabel();
  }

  /** Scrolling decode-activity graph (30 s window): per-bucket decoded bytes as
   * accent bars, per-plane decode time as a line, both normalized to the
   * window's own peak. Runs on requestAnimationFrame only while the panel is
   * pinned; prefers-reduced-motion drops it to one redraw per second. */
  function drawDecodeGraph(): void {
    const context = statGraph.getContext("2d");
    if (!context) return;
    const width = statGraph.clientWidth;
    const height = statGraph.clientHeight;
    if (width === 0 || height === 0) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (statGraph.width !== Math.round(width * dpr) || statGraph.height !== Math.round(height * dpr)) {
      statGraph.width = Math.round(width * dpr);
      statGraph.height = Math.round(height * dpr);
    }
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);

    const now = performance.now();
    const buckets = Math.ceil(DECODE_GRAPH_WINDOW_MS / DECODE_GRAPH_BUCKET_MS);
    const bytesPerBucket = new Array<number>(buckets).fill(0);
    let peakMs = 0;
    for (const event of decodeEvents) {
      const age = now - event.at;
      if (age < 0 || age >= DECODE_GRAPH_WINDOW_MS) continue;
      bytesPerBucket[buckets - 1 - Math.floor(age / DECODE_GRAPH_BUCKET_MS)]! += event.bytes;
      peakMs = Math.max(peakMs, event.ms);
    }
    const peakBytes = Math.max(...bytesPerBucket);

    const styles = getComputedStyle(document.body);
    const accent = styles.getPropertyValue("--accent").trim() || "#54d6c7";
    const mutedColor = styles.getPropertyValue("--muted").trim() || "#8ca6b2";
    const textColor = styles.getPropertyValue("--text").trim() || "#eaf4f6";

    const barWidth = width / buckets;
    if (peakBytes > 0) {
      context.globalAlpha = 0.6;
      context.fillStyle = accent;
      for (const [index, bytes] of bytesPerBucket.entries()) {
        if (bytes === 0) continue;
        const barHeight = Math.max(1, (bytes / peakBytes) * (height - 12));
        context.fillRect(index * barWidth, height - barHeight, Math.max(1, barWidth - 0.5), barHeight);
      }
      context.globalAlpha = 1;
    }

    if (peakMs > 0) {
      context.strokeStyle = textColor;
      context.globalAlpha = 0.85;
      context.lineWidth = 1;
      context.beginPath();
      let started = false;
      for (const event of decodeEvents) {
        const age = now - event.at;
        if (age < 0 || age >= DECODE_GRAPH_WINDOW_MS) continue;
        const x = (1 - age / DECODE_GRAPH_WINDOW_MS) * width;
        const y = 10 + (1 - event.ms / peakMs) * (height - 12);
        if (started) context.lineTo(x, y);
        else context.moveTo(x, y);
        started = true;
      }
      context.stroke();
      context.globalAlpha = 1;
    }

    context.fillStyle = mutedColor;
    context.font = "8px 'IBM Plex Mono', monospace";
    context.textBaseline = "top";
    context.textAlign = "left";
    context.fillText(`${formatBytes(peakBytes * (1000 / DECODE_GRAPH_BUCKET_MS))}/s`, 3, 3);
    context.textAlign = "right";
    context.fillText(peakMs > 0 ? `${peakMs.toFixed(1)} ms` : "idle", width - 3, 3);
  }

  let statsGraphFrame: number | null = null;
  let lastGraphDrawAt = 0;

  function statsGraphLoop(timestamp: number): void {
    statsGraphFrame = null;
    if (!statsVisible()) return;
    if (timestamp - lastGraphDrawAt >= (reducedMotion.matches ? 1_000 : 0)) {
      lastGraphDrawAt = timestamp;
      drawDecodeGraph();
      // The rate row rides the same clock as the graph it summarizes.
      statDecodeRate.textContent = `${formatBytes(decodeRateBytesPerSec())}/s`;
    }
    statsGraphFrame = window.requestAnimationFrame(statsGraphLoop);
  }

  function startStatsGraph(): void {
    if (statsGraphFrame === null && statsVisible()) {
      statsGraphFrame = window.requestAnimationFrame(statsGraphLoop);
    }
  }

  /** YouTube「复制调试信息」: one plain-text snapshot of everything the stats
   * panel knows, for bug reports. */
  function debugInfoText(): string {
    const session = deps.activeSession;
    const time = deps.metadata?.time;
    const lines = [
      `xue-debug ${new Date().toISOString()}`,
      `dataset: ${deps.currentRun ? `${deps.selectedModelId}.${deps.currentRun}` : "--"}`,
      `variable: ${session ? `${session.id} (${session.variable.unit})` : "--"}`,
      `format: ${session ? formatReadout(session) : "--"}`,
      `lines: ${overlaySlots().map((slot) => `${slot.session!.id} (${formatReadout(slot.session!)})`).join(", ") || "--"}`,
      `grid: ${session ? `${session.metadata.grid.width} × ${session.metadata.grid.height}` : "--"}`,
      `time: ${time ? `${time.frameCount}F · first ${time.firstForecastHour}h · ${time.stepHours !== undefined ? `step ${time.stepHours}h` : "mixed step"}` : "--"}`,
      `frame: ${deps.activeFrameIndex === null ? "--" : formatLead(deps.activeFrameIndex)} · ${deps.playbackFps} fps`,
      `planes: ${cachedFrameCount()} / ${frameCount()} · ${formatBytes(deps.planeCacheBytes)} / ${formatBytes(planeCacheBudgetBytes())} · window ${prefetchWindowFrames()}F`,
      `network: ${session ? `${formatBytes(session.bytes)} / ${formatBytes(session.totalBytes)}${session.resident ? " · resident" : session.streaming ? " · streaming" : ""}` : "--"}`,
      `decode: ${deps.lastDecodeMs === null ? "--" : `${deps.lastDecodeMs.toFixed(1)} ms`} · ${formatBytes(decodeRateBytesPerSec())}/s`,
      `viewport: ${Math.round(neededGridWidth())} col${tileShare()} · zoom ${map.getZoom().toFixed(2)} · dpr ${window.devicePixelRatio || 1}`,
      `connection: ${connectionLabel()}`,
      `clock: ${displayZone} · ${zonePref}${deps.probePanelControl.probeZone ? " (pinned)" : ""}`,
      `ua: ${navigator.userAgent}`,
    ];
    return lines.join("\n");
  }

  async function copyDebugInfo(): Promise<void> {
    const previous = loadStatus.textContent;
    let notice = t("debugCopied");
    try {
      await navigator.clipboard.writeText(debugInfoText());
    } catch {
      notice = t("copyFailed");
    }
    // A passing notice: the status underneath keeps its message, so a language
    // switch during it restores the right one.
    loadStatus.textContent = notice;
    window.setTimeout(() => {
      if (loadStatus.textContent === notice) loadStatus.textContent = previous;
    }, 1600);
  }

  function hideContextMenu(): void {
    contextMenu.hidden = true;
  }

  function showContextMenu(x: number, y: number): void {
    contextMenu.hidden = false;
    const rect = contextMenu.getBoundingClientRect();
    contextMenu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - rect.width - 8))}px`;
    contextMenu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - rect.height - 8))}px`;
  }

  return {
    updateStatsReadout,
    showContextMenu,
    hideContextMenu,
    setStatsVisible,
    statsVisible,
    copyDebugInfo,
    STATS_VISIBLE_KEY,
  };
}
