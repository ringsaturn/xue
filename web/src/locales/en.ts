/** English — the source of truth for the message set.
 *
 * `MessageKey` is `keyof typeof en`, so every other locale module is typed
 * `Record<MessageKey, string>` and `tsc` rejects a missing or stray key. The
 * design notes on each group live here and here only; a translator reads
 * them to know what a string has to fit and what must stay untranslated.
 *
 * Technical diagnostics (thrown Error messages, the worker, debug info) stay
 * English in every locale; only human-facing UI copy lives here. */

export const en = {
  metaDescription:
    "NOAA GFS and ECMWF global 240-hour forecasts of temperature, precipitation, wind, pressure and upper-air humidity and moisture transport",
  mapAria: "Global temperature and precipitation forecast map",
  modelSwitchAria: "Forecast model",
  variableSwitchAria: "Weather variable",
  /** The variable buttons' small label, carried visually hidden beside the
   * code for the accessible name: in Chinese it glosses the English code
   * (TEMP 气温); repeating the code in English would be redundant, so there
   * it carries the instrument detail instead (TEMP 2M), matching the model
   * buttons' code + detail structure. Keep a translation short — it reads
   * aloud right after the code. */
  varTemp: "2M",
  varPrecip: "RATE",
  varWind: "10M",
  varSolar: "FLUX",
  varRadar: "CREF",
  /** The surface diagnostics: gust and CAPE are surface fields (SFC,
   * the same detail the codes carry), cloud cover is the whole column. */
  varGust: "SFC",
  varCloud: "TOTAL",
  varCape: "SFC",
  /** Visibility is a surface field; the dew point and apparent temperature
   * are 2 m fields; the two upper-air families name their quantity. */
  varVis: "SFC",
  varDewPoint: "2M",
  varFeelsLike: "2M",
  varOmega: "VERT",
  varThetaE: "EQUIV",
  /** The upper-air families' tiles: relative humidity and the water vapour
   * flux have no surface member, so the gloss names the field rather than a
   * level. */
  varHumidity: "REL",
  varVapourFlux: "FLUX",
  // The pressure family's buttons carry the level in the code ("MSLP",
  // "500MB"); the gloss says what the field is, once for all eight heights.
  varPressure: "SEA LVL",
  varHeight: "HEIGHT",
  /** The rail tile standing for the whole pressure family; the surface
   * itself is picked on the timeline capsule's level row. */
  varPressureField: "FIELD",
  /** The gloss on a generic rail tile: a bundle the run publishes that this
   * build has no tile of its own for. Before it is opened the id is all
   * there is to say about it. */
  varUnknownLayer: "LAYER",

  /** The layer rail shows one character per layer — a single-glyph name in
   * the CJK locales, or the Latin symbol the field is written with (Z for
   * radar reflectivity) everywhere else. These are WMO symbols, not words:
   * a locale whose script has no conventional single glyph keeps them. The
   * code and its gloss ride along, visually hidden, so each tile still names
   * itself. */
  glyphTmp2m: "T",
  glyphPrate: "R",
  glyphWind10m: "W",
  glyphDswrf: "S",
  glyphCref: "Z",
  // N is the synoptic-code letter for total cloud amount; the gust and CAPE
  // take their initials.
  glyphGust: "G",
  glyphTcdc: "N",
  glyphCape: "C",
  // ω and θ are the symbols the two upper-air quantities are written with
  // in every script; the surface fields take their initials.
  glyphVis: "V",
  glyphDpt2m: "D",
  glyphAptmp2m: "A",
  glyphVvel: "ω",
  glyphThetae: "θ",
  glyphPressure: "P",
  // U is the WMO symbol for relative humidity; the vapour flux takes the F
  // of flux.
  glyphRh: "U",
  glyphQflux: "F",
  /** The rail tiles' hover tooltip: the full name of the layer, in plain
   * words, since an icon and a WMO letter do not explain themselves. A
   * family tile names the quantity (the level row picks the surface); a
   * single layer reuses its data-card label (`varLabel*`). */
  tipTemp: "Temperature",
  tipHumidity: "Relative humidity",
  tipWind: "Wind",
  tipPressure: "Pressure and geopotential height",
  tipVapourFlux: "Water vapour flux",
  tipCloud: "Cloud cover",
  tipOmega: "Vertical velocity",
  tipThetaE: "Equivalent potential temperature",
  levelSwitchAria: "Level",
  levelCaption: "LEVEL",
  /** The level row's caption for the lines' group when the fill has a level
   * row of its own beside it. */
  linesCaption: "LINES",
  modelHeading: "FORECAST MODEL",
  /** The pressed lines member over a filled field is the overlay's off
   * switch; the tooltip says so, since the × alone is a hint. */
  linesRemoveTitle: "Press again to remove the lines",
  /** The credits: a one-line footer on desktop, and on phones a trigger in
   * its corner opening a sheet with one row per source. The row names are
   * proper nouns and stay as they are; the glosses are translated. They are
   * uppercase code words here, and stay uppercase wherever the script has
   * case. */
  creditsTrigger: "SOURCES",
  creditsHeading: "DATA & CREDITS",
  creditsGfs: "FORECAST DATA · GFS 0.25° AND SFLUX",
  creditsEcmwf: "CONTAINS MODIFIED ECMWF DATA · CC BY 4.0",
  creditsRadar: "RADAR MOSAIC",
  creditsBasemap: "BASEMAP · © OPENSTREETMAP CONTRIBUTORS",
  creditsCode: "SOURCE CODE · RINGSATURN/XUE",
  caseTag: "CASE",
  runCycle: "Model run",
  // An observation dataset has no run cycle and no lead time: its runTime is
  // when the series starts, and a frame is an observation, not a forecast.
  seriesStart: "Series start",
  awaitingData: "Awaiting data",
  validTimeLabel: "Valid time",
  observationTimeLabel: "Observation time",
  statsAria: "Stats for nerds",
  statsCloseAria: "Close stats",
  bufferedFrames: "Buffered",
  downloadedBytes: "Downloaded",
  formatLabel: "Format",
  preloadProgressAria: "Forecast data preload progress",
  statDataset: "Dataset",
  statGrid: "Grid",
  statFrameCache: "Frame cache",
  statDecodeTime: "Decode time",
  statDecodeRate: "Decode rate",
  statViewport: "Viewport sampling",
  statConnection: "Connection",
  awaitingManifest: "Awaiting manifest",
  legendPrateAria: "Precipitation rate color scale",
  timelineAria: "Forecast time controls",
  playAnimation: "Play animation",
  pauseAnimation: "Pause animation",
  /** The speed button reads "12 FPS" in every locale (an instrument value,
   * like the forecast hour); only its accessible name is translated. */
  playbackSpeed: "Playback speed",
  readingManifest: "Loading manifest",
  forecastHourAria: "Forecast hour",
  elapsedAria: "Time elapsed",
  forecastDaysAria: "Daily forecast segments",
  observationDaysAria: "Daily segments",
  mapMenuAria: "Map options",
  menuStats: "Stats for nerds",
  menuCopyDebug: "Copy debug info",
  errorTitle: "Data unavailable",
  retry: "Retry",
  /** The language picker: a round icon button in the top-right column and a
   * panel of endonyms under it. The endonyms themselves are never
   * translated — each language names itself, in its own script — so the only
   * copy here is the word "Language" on the trigger and the panel heading.
   * The heading is a sheet heading, so it keeps the uppercase code-word
   * style the model sheet's does. */
  langPickerAria: "Language",
  langPickerHeading: "LANGUAGE",
  themeToggleAria: "Toggle light / dark",
  /** The wind layer's own control, in the transport capsule: the colored
   * speed field is always drawn, and this turns the particle animation over
   * it on and off. Icon-only, like the three circles above, so the name is
   * carried by the accessible label and a visually hidden word. */
  particlesToggle: "Particles",
  particlesToggleAria: "Toggle wind particle animation",

  dataInterrupted: "Data loading interrupted",
  errorHint: "{message}. Make sure make mvp has been run and the page is served via make serve.",
  loadFailed: "Load failed",
  saveData: "saver",
  debugCopied: "Debug info copied",
  copyFailed: "Copy failed",
  bundleResident: "Bundle fully buffered",
  // A narrowed view fetches only the tiles it shows, so it never reaches
  // whole-bundle residency — and saying so beats a progress bar that stalls.
  viewportResident: "Viewport fully buffered",
  streamingOnDemand: "Streaming on demand",
  receivingBundle: "Receiving forecast bundle",
  receivingBundlePercent: "Receiving bundle {percent}%",
  readingIndex: "Reading data index",
  initializingDecoder: "Initializing decoder",
  framesReady: "{count} frames ready",
  readingData: "Loading data",
  bundleLoadFailed: "Forecast bundle failed to load",
  workerStartFailed: "Decode worker failed to start",
  decodeFailed: "Decode failed",
  frameDecodeFailed: "Frame decode failed: {message}",
  legendAria: "{label} color scale",
  varLabelTmp2m: "2 m temperature",
  varLabelPrate: "Precipitation rate",
  varLabelDswrf: "Solar radiation",
  varLabelWind10m: "10 m wind",
  varLabelCref: "Composite radar reflectivity",
  varLabelGust: "Wind gust",
  varLabelTcdc: "Total cloud cover",
  varLabelCape: "Convective available potential energy",
  varLabelVis: "Visibility",
  varLabelDpt2m: "2 m dew point",
  varLabelAptmp2m: "2 m apparent temperature",
  varLabelLcdc: "Low cloud cover",
  varLabelMcdc: "Middle cloud cover",
  varLabelHcdc: "High cloud cover",
  varLabelPrmsl: "Mean sea level pressure",
  // One string for eight levels: pressure.ts and levels.ts substitute {level}.
  varLabelHeightAtLevel: "{level} hPa geopotential height",
  varLabelTempAtLevel: "{level} hPa temperature",
  varLabelRhAtLevel: "{level} hPa relative humidity",
  varLabelSpfhAtLevel: "{level} hPa specific humidity",
  varLabelWindAtLevel: "{level} hPa wind",
  varLabelQfluxAtLevel: "{level} hPa water vapour flux",
  varLabelVvelAtLevel: "{level} hPa vertical velocity",
  varLabelThetaeAtLevel: "{level} hPa equivalent potential temperature",
  // The 5880 gpm contour, "588" on a Chinese chart — the line the western
  // Pacific subtropical high is defined by.
  legendSubtropicalHigh: "588 line",
  webglUnavailable: "This browser does not provide a WebGL2 context",
  pointerRequestFailed: "Live pointer request returned HTTP {status}",
  manifestRequestFailed: "Manifest request returned HTTP {status}",
  bundleRequestFailed: "Bundle request returned HTTP {status}",
  videoIndexRequestFailed: "Video index request returned HTTP {status}",
  bundleTooLong: "Bundle exceeds the length declared by the manifest",
  bundleLengthMismatch: "Bundle length does not match the manifest",
  bundleChecksumMismatch: "Bundle checksum mismatch",
  bundleRunMismatch: "Bundle run cycle does not match the manifest",
  bundleMissingVariable: "Bundle is missing variable {id}",
  manifestMissingBundle: "Manifest has no bundle for variable {id}",

  // The point probe: click the map to read one grid cell across the axis.
  // Its heading ("POINT") and every number stay English, like the rest of
  // the instrument panel; only the prose below the chart is translated.
  probeAria: "Point data series",
  probeHint: "Play or scrub to fill the series",
  probeComplete: "Series complete",
  probeOutside: "Outside this dataset's grid",
  probeNoData: "No data at this frame",
  probeAwaiting: "Waiting for this frame",

  // The letter on a pressure center, following each country's own chart
  // convention: H/L in English, 高/低 on a Chinese or Japanese chart, 고/저
  // in Korean, H/T in German, A/D in French, A/B in Spanish and Portuguese,
  // В/Н in Russian. Either way it is the one glyph on the map that is not a
  // number.
  centerHigh: "H",
  centerLow: "L",

  // Historical showcase: the list page and the viewer's case banner. The
  // footer link keeps the footer's code-word style in English.
  showcaseLink: "CASES",
  showcaseBack: "← All cases",
  showcaseHome: "LIVE FORECAST",
  showcaseTitle: "Showcase cases",
  showcaseHeading: "Weather events, replayed",
  /** The italic sub-title beside the heading. */
  showcaseArchive: "archive",
  showcaseIntro:
    "Each case is a slice of one past weather event — an archived forecast run, or observations — through the same encoder, cropped to the region and hours it happened in.",
  showcaseMetaDescription:
    "Replays of past weather events: cropped forecast and radar datasets for typhoons, rainstorms, heatwaves and more",
  showcaseLoading: "Loading case catalog",
  showcaseEmpty: "No cases published yet",
  showcaseLoadFailed: "Case catalog failed to load: {message}",
  showcaseCaseMissing: "No showcase case named {id}",
  showcaseEventLabel: "Event",
  showcaseRunLabel: "Run",
  showcaseSeriesLabel: "Start",
  showcaseRangeLabel: "Range",
  showcaseSpanLabel: "Span",
  showcaseRegionLabel: "Region",
  showcaseGridLabel: "Grid",
  showcaseSizeLabel: "Size",
  showcaseVariablesLabel: "Fields",
  showcaseHours: "{count} h",
  showcaseCaseAria: "Open case {title}",
  showcaseListAria: "Showcase case list",
} as const;

/** Every key the UI can ask for. Each locale module is typed against this,
 * so a missing or stray key is a compile error rather than a blank label. */
export type MessageKey = keyof typeof en;
