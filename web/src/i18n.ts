/** UI locale support: Chinese and English. The locale is fixed per page load —
 * detection order is the `?lang=` URL param, then the persisted toggle choice,
 * then the browser language — and the basemap label language follows it.
 * Technical diagnostics (thrown Error messages, the worker, debug info) stay
 * English in both locales; only human-facing UI copy lives here. */

export type Locale = "zh" | "en";

const STORAGE_KEY = "xue-locale";

function normalize(value: string | null | undefined): Locale | null {
  if (!value) return null;
  const lower = value.toLowerCase();
  if (lower === "zh" || lower.startsWith("zh-")) return "zh";
  if (lower === "en" || lower.startsWith("en-")) return "en";
  return null;
}

function detectLocale(): Locale {
  // Unit tests import this module under jsdom/node; never require a browser.
  if (typeof window === "undefined") return "en";
  const fromUrl = normalize(new URLSearchParams(window.location.search).get("lang"));
  if (fromUrl) return fromUrl;
  try {
    const stored = normalize(localStorage.getItem(STORAGE_KEY));
    if (stored) return stored;
  } catch {
    // Storage can be unavailable (privacy modes); browser language decides.
  }
  for (const tag of navigator.languages ?? [navigator.language]) {
    const match = normalize(tag);
    if (match) return match;
  }
  return "en";
}

export const locale: Locale = detectLocale();

/** Language for the Protomaps basemap labels — the map follows the UI. */
export const basemapLang = locale === "zh" ? "zh-Hans" : "en";

/** Value for <html lang>. */
export const htmlLang = locale === "zh" ? "zh-CN" : "en";

const MESSAGES = {
  metaDescription: {
    zh: "NOAA GFS 与 ECMWF 全球未来 120 小时气温、降水、风场与太阳辐射预报",
    en: "NOAA GFS and ECMWF global 120-hour forecasts of temperature, precipitation, wind and solar radiation",
  },
  mapAria: { zh: "全球气温与降水预报地图", en: "Global temperature and precipitation forecast map" },
  modelSwitchAria: { zh: "预报模式", en: "Forecast model" },
  variableSwitchAria: { zh: "气象变量", en: "Weather variable" },
  /** The variable buttons' small label: in Chinese it glosses the English
   * code (TEMP 气温); repeating the code in English would be redundant, so
   * there it carries the instrument detail instead (TEMP 2M), matching the
   * model buttons' code + detail structure. */
  varTemp: { zh: "气温", en: "2M" },
  varPrecip: { zh: "降水", en: "RATE" },
  varWind: { zh: "风场", en: "10M" },
  varSolar: { zh: "辐射", en: "FLUX" },
  runCycle: { zh: "模式周期", en: "Model run" },
  awaitingData: { zh: "等待数据", en: "Awaiting data" },
  validTimeLabel: { zh: "当前有效时间", en: "Valid time" },
  statsAria: { zh: "详细统计信息", en: "Stats for nerds" },
  statsCloseAria: { zh: "关闭详细统计信息", en: "Close stats" },
  bufferedFrames: { zh: "缓存帧", en: "Buffered" },
  downloadedBytes: { zh: "数据量", en: "Downloaded" },
  formatLabel: { zh: "格式", en: "Format" },
  preloadProgressAria: { zh: "预报数据预加载进度", en: "Forecast data preload progress" },
  statDataset: { zh: "数据集", en: "Dataset" },
  statGrid: { zh: "网格", en: "Grid" },
  statFrameCache: { zh: "帧缓存", en: "Frame cache" },
  statDecodeTime: { zh: "解码耗时", en: "Decode time" },
  statDecodeRate: { zh: "解码速率", en: "Decode rate" },
  statViewport: { zh: "视口采样", en: "Viewport sampling" },
  statConnection: { zh: "网络连接", en: "Connection" },
  awaitingManifest: { zh: "等待数据舱单", en: "Awaiting manifest" },
  legendPrateAria: { zh: "降水强度色标", en: "Precipitation rate color scale" },
  timelineAria: { zh: "预报时间控制", en: "Forecast time controls" },
  playAnimation: { zh: "播放动画", en: "Play animation" },
  pauseAnimation: { zh: "暂停动画", en: "Pause animation" },
  readingManifest: { zh: "正在读取清单", en: "Loading manifest" },
  expandTimeline: { zh: "展开时间轴详情", en: "Expand timeline details" },
  collapseTimeline: { zh: "收起时间轴详情", en: "Collapse timeline details" },
  forecastHourAria: { zh: "预报时次", en: "Forecast hour" },
  forecastDaysAria: { zh: "五日预报分段", en: "Five-day forecast segments" },
  mapMenuAria: { zh: "地图选项", en: "Map options" },
  menuStats: { zh: "详细统计信息", en: "Stats for nerds" },
  menuCopyDebug: { zh: "复制调试信息", en: "Copy debug info" },
  errorTitle: { zh: "数据无法显示", en: "Data unavailable" },
  retry: { zh: "重新读取", en: "Retry" },
  /** The toggle names the language it switches TO. */
  langToggle: { zh: "ENGLISH", en: "中文" },

  dataInterrupted: { zh: "数据加载中断", en: "Data loading interrupted" },
  errorHint: {
    zh: "{message}。请确认已运行 make mvp,并通过 make serve 打开页面。",
    en: "{message}. Make sure make mvp has been run and the page is served via make serve.",
  },
  loadFailed: { zh: "加载失败", en: "Load failed" },
  saveData: { zh: "省流", en: "saver" },
  debugCopied: { zh: "调试信息已复制", en: "Debug info copied" },
  copyFailed: { zh: "复制失败", en: "Copy failed" },
  bundleResident: { zh: "数据包已驻留内存", en: "Bundle fully buffered" },
  streamingOnDemand: { zh: "按需流式加载中", en: "Streaming on demand" },
  receivingBundle: { zh: "正在接收预报数据包", en: "Receiving forecast bundle" },
  receivingBundlePercent: { zh: "接收数据包 {percent}%", en: "Receiving bundle {percent}%" },
  readingIndex: { zh: "正在读取数据索引", en: "Reading data index" },
  initializingDecoder: { zh: "正在初始化解码器", en: "Initializing decoder" },
  framesReady: { zh: "{count} 帧就绪", en: "{count} frames ready" },
  readingData: { zh: "正在读取数据", en: "Loading data" },
  bundleLoadFailed: { zh: "预报数据包加载失败", en: "Forecast bundle failed to load" },
  workerStartFailed: { zh: "解码 Worker 启动失败", en: "Decode worker failed to start" },
  decodeFailed: { zh: "解码失败", en: "Decode failed" },
  frameDecodeFailed: { zh: "帧解码失败:{message}", en: "Frame decode failed: {message}" },
  legendAria: { zh: "{label}色标", en: "{label} color scale" },
  varLabelTmp2m: { zh: "2 米气温", en: "2 m temperature" },
  varLabelPrate: { zh: "降水强度", en: "Precipitation rate" },
  varLabelDswrf: { zh: "太阳辐射", en: "Solar radiation" },
  varLabelWind10m: { zh: "10 米风", en: "10 m wind" },
  webglUnavailable: {
    zh: "此浏览器未提供 WebGL2 上下文",
    en: "This browser does not provide a WebGL2 context",
  },
  pointerRequestFailed: { zh: "直播指针请求返回 HTTP {status}", en: "Live pointer request returned HTTP {status}" },
  manifestRequestFailed: { zh: "清单请求返回 HTTP {status}", en: "Manifest request returned HTTP {status}" },
  bundleRequestFailed: { zh: "数据包请求返回 HTTP {status}", en: "Bundle request returned HTTP {status}" },
  videoIndexRequestFailed: { zh: "视频索引请求返回 HTTP {status}", en: "Video index request returned HTTP {status}" },
  bundleTooLong: { zh: "数据包超出清单声明的长度", en: "Bundle exceeds the length declared by the manifest" },
  bundleLengthMismatch: { zh: "数据包长度与清单不一致", en: "Bundle length does not match the manifest" },
  bundleChecksumMismatch: { zh: "数据包校验和不一致", en: "Bundle checksum mismatch" },
  bundleRunMismatch: { zh: "数据包运行周期与清单不一致", en: "Bundle run cycle does not match the manifest" },
  bundleMissingVariable: { zh: "数据包缺少变量 {id}", en: "Bundle is missing variable {id}" },
  manifestMissingBundle: { zh: "清单缺少变量 {id} 的数据包", en: "Manifest has no bundle for variable {id}" },
} as const;

export type MessageKey = keyof typeof MESSAGES;

export function t(key: MessageKey, params?: Record<string, string | number>): string {
  let text: string = MESSAGES[key][locale];
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      text = text.replace(`{${name}}`, String(value));
    }
  }
  return text;
}

/** Rewrites every element carrying data-i18n / data-i18n-aria / data-i18n-content
 * from the dictionary, and stamps <html lang> and the meta description. The
 * markup ships the English copy as its pre-JS fallback. */
export function applyStaticMessages(): void {
  document.documentElement.lang = htmlLang;
  for (const element of document.querySelectorAll<HTMLElement>("[data-i18n]")) {
    element.textContent = t(element.dataset.i18n as MessageKey);
  }
  for (const element of document.querySelectorAll<HTMLElement>("[data-i18n-aria]")) {
    element.setAttribute("aria-label", t(element.dataset.i18nAria as MessageKey));
  }
  for (const element of document.querySelectorAll<HTMLElement>("[data-i18n-content]")) {
    element.setAttribute("content", t(element.dataset.i18nContent as MessageKey));
  }
}

/** Persists the other locale and reloads onto it (the URL keeps model/type,
 * and the explicit ?lang= makes the resulting page shareable as-is). */
export function toggleLocale(): void {
  const next: Locale = locale === "zh" ? "en" : "zh";
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // The URL param below still carries the choice.
  }
  const params = new URLSearchParams(window.location.search);
  params.set("lang", next);
  window.location.search = `?${params.toString()}`;
}
