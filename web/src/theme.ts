/**
 * Light / dark appearance.
 *
 * Like the locale, the theme is fixed per page load — detection order is the
 * `?theme=` URL param, then the persisted toggle choice, then the operating
 * system's `prefers-color-scheme`, and paper (light) when nothing says
 * otherwise. `index.html` runs the same three steps inline before the first
 * paint; this module is the version the rest of the app reads.
 *
 * Toggling reloads, exactly as the locale toggle does. The basemap's colors
 * come from a Protomaps style flavor, and swapping a flavor means
 * `map.setStyle`, which drops the custom WebGL layers the forecast is drawn
 * in — reloading is both simpler and the only way the map, the shader's
 * contour ink and the CSS tokens are guaranteed to agree.
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

export const theme: Theme = detectTheme();
export const isDark = theme === "dark";

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

/** Persists the other theme and reloads onto it (the URL keeps model/type,
 * and the explicit ?theme= makes the resulting page shareable as-is). */
export function toggleTheme(): void {
  const next: Theme = isDark ? "light" : "dark";
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // The URL param below still carries the choice.
  }
  const params = new URLSearchParams(window.location.search);
  params.set("theme", next);
  window.location.search = `?${params.toString()}`;
}
