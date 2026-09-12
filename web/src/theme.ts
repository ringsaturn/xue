/**
 * Light / dark appearance.
 *
 * Like the locale, the theme is resolved once before the first render —
 * detection order is the `?theme=` URL param, then the persisted toggle
 * choice, then the operating system's `prefers-color-scheme`, and paper
 * (light) when nothing says otherwise. `index.html` runs the same three steps
 * inline before the first paint; this module is the version the rest of the
 * app reads.
 *
 * Toggling never reloads. The stylesheet carries both palettes under
 * `data-theme`, and everything the shell draws itself — the basemap's flavor
 * colors, the contour ink, the particle ink — is repainted in place by the
 * listeners registered through `onThemeChange`, so a session keeps its
 * decoded frames and its playhead across the switch.
 */

export type Theme = "light" | "dark";

const STORAGE_KEY = "xue-theme";

function normalize(value: string | null | undefined): Theme | null {
  return value === "light" || value === "dark" ? value : null;
}

function detectTheme(): Theme {
  // Unit tests import this module under jsdom/node; never require a browser.
  if (typeof window === "undefined") return "light";
  const fromUrl = normalize(new URLSearchParams(window.location.search).get("theme"));
  if (fromUrl) return fromUrl;
  try {
    const stored = normalize(localStorage.getItem(STORAGE_KEY));
    if (stored) return stored;
  } catch {
    // Storage can be unavailable (privacy modes); the OS preference decides.
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** The theme in force. A live binding: importers read the current value,
 * and anything captured from it at module load has to be re-read on
 * `onThemeChange`. */
export let theme: Theme = detectTheme();
export let isDark = theme === "dark";

const listeners = new Set<() => void>();

/** Run `listener` after every toggle, once the document already carries the
 * new theme; the listener repaints whatever it owns from `isDark`. */
export function onThemeChange(listener: () => void): void {
  listeners.add(listener);
}

/** Stamp the resolved theme on <html> and narrow the theme-color meta to it.
 * The inline head script has usually done the first half already; this makes
 * the choice explicit even when it came from the OS preference, so nothing
 * downstream has to re-evaluate the media query. */
export function applyTheme(): void {
  document.documentElement.dataset.theme = theme;
  for (const meta of document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')) {
    meta.remove();
  }
  const meta = document.createElement("meta");
  meta.name = "theme-color";
  meta.content = isDark ? "#000000" : "#F3EFE6";
  document.head.append(meta);
}

/** Switch to the other theme in place: persist it, write the explicit
 * `?theme=` into the address bar (so the page is shareable as-is), restamp
 * the document and let every listener repaint. */
export function toggleTheme(): void {
  theme = isDark ? "light" : "dark";
  isDark = theme === "dark";
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // The URL param below still carries the choice.
  }
  const params = new URLSearchParams(window.location.search);
  params.set("theme", theme);
  window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}${window.location.hash}`);
  applyTheme();
  for (const listener of listeners) listener();
}
