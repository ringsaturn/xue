/** UI locale support: Chinese and English. The locale is fixed per page load —
 * detection order is the `?lang=` URL param, then the choice the toggle
 * stored on this device, then the browser language — and the basemap label
 * language follows it. Only the URL param is ever explicit; a shared link
 * carries none unless written by hand, so it opens in each reader's own.
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
    zh: "NOAA GFS 与 ECMWF 全球未来 240 小时气温、降水、风场、气压与高空温湿、水汽通量预报",
    en: "NOAA GFS and ECMWF global 240-hour forecasts of temperature, precipitation, wind, pressure and upper-air humidity and moisture transport",
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
  varRadar: { zh: "雷达", en: "CREF" },
  /** The upper-air families' tiles: relative humidity, specific humidity and
   * the water vapour flux have no surface member, so the gloss names the
   * field rather than a level. */
  varHumidity: { zh: "湿度", en: "REL" },
  varSpecificHumidity: { zh: "比湿", en: "SPEC" },
  varVapourFlux: { zh: "水汽", en: "FLUX" },
  // The pressure family's buttons carry the level in the code ("MSLP",
  // "500MB"); the gloss says what the field is, once for all eight heights.
  varPressure: { zh: "气压", en: "SEA LVL" },
  varHeight: { zh: "位势", en: "HEIGHT" },
  /** The rail tile standing for the whole pressure family; the surface
   * itself is picked on the timeline capsule's level row. */
  varPressureField: { zh: "气压场", en: "FIELD" },

  /** The layer rail shows one character per layer — a Chinese single-glyph
   * name, or the Latin symbol the field is written with (Z for radar
   * reflectivity). The code and its gloss ride along, visually hidden, so
   * each tile still names itself. */
  glyphTmp2m: { zh: "温", en: "T" },
  glyphPrate: { zh: "雨", en: "R" },
  glyphWind10m: { zh: "风", en: "W" },
  glyphDswrf: { zh: "辐", en: "S" },
  glyphCref: { zh: "雷", en: "Z" },
  glyphPressure: { zh: "压", en: "P" },
  // U is the WMO symbol for relative humidity, q for specific humidity; the
  // vapour flux takes the F of flux.
  glyphRh: { zh: "湿", en: "U" },
  glyphSpfh: { zh: "比", en: "q" },
  glyphQflux: { zh: "汽", en: "F" },
  levelSwitchAria: { zh: "层次", en: "Level" },
  levelCaption: { zh: "层次", en: "LEVEL" },
  /** The level row's caption for the lines' group when the fill has a level
   * row of its own beside it. */
  linesCaption: { zh: "等压线", en: "LINES" },
  modelHeading: { zh: "预报模式", en: "FORECAST MODEL" },
  caseTag: { zh: "个例", en: "CASE" },
  runCycle: { zh: "模式周期", en: "Model run" },
  // An observation dataset has no run cycle and no lead time: its runTime is
  // when the series starts, and a frame is an observation, not a forecast.
  seriesStart: { zh: "观测起点", en: "Series start" },
  awaitingData: { zh: "等待数据", en: "Awaiting data" },
  validTimeLabel: { zh: "当前有效时间", en: "Valid time" },
  observationTimeLabel: { zh: "当前观测时间", en: "Observation time" },
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
  /** The speed button reads "12 FPS" in both locales (an instrument value,
   * like the forecast hour); only its accessible name is translated. */
  playbackSpeed: { zh: "播放速度", en: "Playback speed" },
  readingManifest: { zh: "正在读取清单", en: "Loading manifest" },
  forecastHourAria: { zh: "预报时次", en: "Forecast hour" },
  elapsedAria: { zh: "观测时长", en: "Time elapsed" },
  forecastDaysAria: { zh: "逐日预报分段", en: "Daily forecast segments" },
  observationDaysAria: { zh: "逐日分段", en: "Daily segments" },
  mapMenuAria: { zh: "地图选项", en: "Map options" },
  menuStats: { zh: "详细统计信息", en: "Stats for nerds" },
  menuCopyDebug: { zh: "复制调试信息", en: "Copy debug info" },
  errorTitle: { zh: "数据无法显示", en: "Data unavailable" },
  retry: { zh: "重新读取", en: "Retry" },
  /** Both name the language the toggle switches TO. The button is a 44px
   * circle, so the visible half is one character. */
  langToggleShort: { zh: "EN", en: "中" },
  langToggleAria: { zh: "Switch to English", en: "切换到中文" },
  themeToggleAria: { zh: "切换浅色 / 深色", en: "Toggle light / dark" },
  /** The wind layer's own control, in the transport capsule: the colored
   * speed field is always drawn, and this turns the particle animation over
   * it on and off. Icon-only, like the three circles above, so the name is
   * carried by the accessible label and a visually hidden word. */
  particlesToggle: { zh: "粒子动画", en: "Particles" },
  particlesToggleAria: { zh: "切换风场粒子动画", en: "Toggle wind particle animation" },

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
  // A narrowed view fetches only the tiles it shows, so it never reaches
  // whole-bundle residency — and saying so beats a progress bar that stalls.
  viewportResident: { zh: "当前视野已缓冲完毕", en: "Viewport fully buffered" },
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
  varLabelCref: { zh: "雷达组合反射率", en: "Composite radar reflectivity" },
  varLabelPrmsl: { zh: "海平面气压", en: "Mean sea level pressure" },
  // One string for eight levels: pressure.ts and levels.ts substitute {level}.
  varLabelHeightAtLevel: { zh: "{level} 百帕位势高度", en: "{level} hPa geopotential height" },
  varLabelTempAtLevel: { zh: "{level} 百帕气温", en: "{level} hPa temperature" },
  varLabelRhAtLevel: { zh: "{level} 百帕相对湿度", en: "{level} hPa relative humidity" },
  varLabelSpfhAtLevel: { zh: "{level} 百帕比湿", en: "{level} hPa specific humidity" },
  varLabelWindAtLevel: { zh: "{level} 百帕风", en: "{level} hPa wind" },
  varLabelQfluxAtLevel: { zh: "{level} 百帕水汽通量", en: "{level} hPa water vapour flux" },
  // The 5880 gpm contour, "588" on a Chinese chart — the line the western
  // Pacific subtropical high is defined by.
  legendSubtropicalHigh: { zh: "588 线", en: "588 line" },
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

  // The point probe: click the map to read one grid cell across the axis.
  // Its heading ("POINT") and every number stay English, like the rest of
  // the instrument panel; only the prose below the chart is translated.
  probeAria: { zh: "点位数据序列", en: "Point data series" },
  probeHint: { zh: "播放或拖动时间轴补全序列", en: "Play or scrub to fill the series" },
  probeComplete: { zh: "序列已完整", en: "Series complete" },
  probeOutside: { zh: "该点在本数据集网格之外", en: "Outside this dataset's grid" },
  probeNoData: { zh: "此帧无数据", en: "No data at this frame" },
  probeAwaiting: { zh: "等待该帧解码", en: "Waiting for this frame" },

  // The letter on a pressure center. A Chinese chart writes the character,
  // an English one the initial; either way it is the one glyph on the map
  // that is not a number.
  centerHigh: { zh: "高", en: "H" },
  centerLow: { zh: "低", en: "L" },

  // Historical showcase: the list page and the viewer's case banner. The
  // footer link keeps the footer's code-word style in English.
  showcaseLink: { zh: "历史个例", en: "CASES" },
  showcaseBack: { zh: "← 全部个例", en: "← All cases" },
  showcaseHome: { zh: "实时预报", en: "LIVE FORECAST" },
  showcaseTitle: { zh: "历史个例", en: "Showcase cases" },
  showcaseHeading: { zh: "天气过程回放", en: "Weather events, replayed" },
  /** The italic sub-title beside the heading. */
  showcaseArchive: { zh: "归档", en: "archive" },
  showcaseIntro: {
    zh: "每个个例都是一段历史天气的切片——历史预报,或实况观测:同一套编码管线,裁切到事件所在的区域和时段。",
    en: "Each case is a slice of one past weather event — an archived forecast run, or observations — through the same encoder, cropped to the region and hours it happened in.",
  },
  showcaseMetaDescription: {
    zh: "历史天气过程回放:台风、暴雨、热浪等个例的裁切数据集,预报与雷达实况",
    en: "Replays of past weather events: cropped forecast and radar datasets for typhoons, rainstorms, heatwaves and more",
  },
  showcaseLoading: { zh: "正在读取个例目录", en: "Loading case catalog" },
  showcaseEmpty: { zh: "暂无历史个例", en: "No cases published yet" },
  showcaseLoadFailed: { zh: "个例目录加载失败:{message}", en: "Case catalog failed to load: {message}" },
  showcaseCaseMissing: { zh: "找不到历史个例 {id}", en: "No showcase case named {id}" },
  showcaseEventLabel: { zh: "过程时间", en: "Event" },
  showcaseRunLabel: { zh: "起报", en: "Run" },
  showcaseSeriesLabel: { zh: "起点", en: "Start" },
  showcaseRangeLabel: { zh: "时效", en: "Range" },
  showcaseSpanLabel: { zh: "时长", en: "Span" },
  showcaseRegionLabel: { zh: "区域", en: "Region" },
  showcaseGridLabel: { zh: "网格", en: "Grid" },
  showcaseSizeLabel: { zh: "数据量", en: "Size" },
  showcaseVariablesLabel: { zh: "变量", en: "Fields" },
  showcaseHours: { zh: "{count} 小时", en: "{count} h" },
  showcaseCaseAria: { zh: "打开个例 {title}", en: "Open case {title}" },
  showcaseListAria: { zh: "历史个例列表", en: "Showcase case list" },
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
/** Switch languages: remember the choice on this device and reload onto it.
 * The choice is deliberately not written into the URL — a link copied
 * afterwards would carry it to everyone it is shared with, overriding their
 * browser's language — and an explicit `?lang=` already on the page is
 * dropped for the same reason, so the reload lands on the stored choice. */
export function toggleLocale(): void {
  const next: Locale = locale === "zh" ? "en" : "zh";
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // Without storage the choice lasts this page load only; the reload
    // below still shows it, since detection falls back to the browser.
  }
  const params = new URLSearchParams(window.location.search);
  params.delete("lang");
  const search = params.size > 0 ? `?${params.toString()}` : "";
  if (search === window.location.search) window.location.reload();
  else window.location.search = search;
}
