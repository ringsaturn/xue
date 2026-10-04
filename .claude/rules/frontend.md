---
paths:
  - "web/**"
  - "tests/web/**"
  - "tests/e2e/**"
  - "vite.config.ts"
  - "playwright.config.ts"
---

# Frontend rules (`web/src/`)

Background: [`docs/contribution/frontend.md`](../../docs/contribution/frontend.md),
[`point-products.md`](../../docs/contribution/point-products.md).

- `web/src/wasm/` is generated (`make wasm`). Nothing typechecks or builds
  without it.
- **Live bindings**: `isDark`, `locale`, `htmlLang`, `basemapLang` and
  `displayZone` change in place. Read them at the point of use and never
  capture them in a module-level constant. Theme and locale switch without a
  reload: subscribe through `onThemeChange` / `onLocaleChange` /
  `onDisplayZoneChange`.
- Never call `map.setStyle`, because it drops the custom WebGL layers. Use
  `syncBasemapStyle`'s property diff.
- Never animate raster opacity. Blend frames through `u_mix`.
- `variables.ts` is built on first use, never at module load: it sits on the
  import cycle identity → variables → levels → pressure → identity.
- **i18n**: eleven locales, one module each under `web/src/locales/`, typed
  against `en.ts` (the source of truth and the only file with design notes).
  Translate human-facing copy only. Thrown errors, worker messages,
  diagnostics and instrument codes (`F058`, `12 FPS`, `PLAY`, `UTC+9`) stay
  English. Long-lived status copy goes through `say()`. Showcase `title` /
  `summary` carry all eleven locales (`tests/fixtures/locales.json`).
- **URL state** is parsed only in `urlstate.ts` (`?lang=` in `i18n.ts`,
  `?theme=` in `theme.ts`). Unknown values fall back to defaults. The camera
  is in the fragment (`#map=`). A new parameter also goes into
  `web/public/llms.txt`.
- What is on screen is one `ViewState` (`viewstate.ts`). The rail, the level
  row and the URL are projections of it. Never keep a second copy.
- The primary session gates the playhead. Overlays, marks (storms,
  stations) and probe sessions never do.
- Nothing keys across sessions by `numericId`: use `sessionkeys.ts`.
- Controls on the right edge share one 44px column at the same offset
  (20px, 16px on phones).
- When `sources.py` changes what a model publishes, update
  `web/public/llms.txt`. Discovery metadata in `index.html`,
  `showcase.html` and `pagemeta.ts` must agree with what is published.
- Locally, the Protomaps key works on `localhost` but not `127.0.0.1`. The
  bucket's CORS allows only port 5173 locally.
- Do not run Playwright locally. CI runs the e2e suite. Run vitest for unit
  tests.
