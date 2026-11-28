/** UI locale support: thirteen languages. The locale is the page's: its
 * path prefix (`/ja/…`, none for English — `sitepaths.ts`), settled before
 * the first render. An unprefixed page has already sent a reader who
 * prefers another language to that one's prefix (the head's inline
 * chooser), so the shell never guesses: the address says the language, and
 * a shared link opens in the language it names. The basemap label language
 * and `<html lang>` follow it.
 *
 * Picking another language never reloads: `setLocale` swaps the dictionary,
 * moves the address to the new prefix, rewrites the static markup and lets
 * every `onLocaleChange` listener redo the copy it owns, so the session
 * keeps its decoded frames and playhead. `locale`, `htmlLang`, `intlLocale`
 * and `basemapLang` are live bindings — read them when needed rather than
 * capturing them at module load.
 *
 * The dictionary itself lives in `locales/`, one module per language, each
 * typed `Record<MessageKey, string>` against `locales/en.ts` — the source of
 * truth, and the only file carrying the design notes on what a string has to
 * fit. Every module is imported statically: the locale is settled before the
 * first render and `t` is synchronous, so there is nothing to await.
 *
 * Technical diagnostics (thrown Error messages, the worker, debug info) stay
 * English in every locale; only human-facing UI copy lives here. */

import { ar } from "./locales/ar";
import { de } from "./locales/de";
import { en, type MessageKey } from "./locales/en";
import { es } from "./locales/es";
import { fr } from "./locales/fr";
import { ja } from "./locales/ja";
import { ko } from "./locales/ko";
import { ne } from "./locales/ne";
import { pt } from "./locales/pt";
import { ru } from "./locales/ru";
import { tr } from "./locales/tr";
import { zh } from "./locales/zh";
import { zhHant } from "./locales/zh-hant";
import { FORECAST_MODEL_IDS } from "./modelpaths";
import {
  casePathname,
  isShellPath,
  legacyLocaleLocation,
  localizedPath,
  normalizeLocale,
  relocalizePath,
  splitLocalePath,
  type Locale,
} from "./sitepaths";
import { isStaticPagePath, pagePrefix } from "./tc/pages";
import { convertTemperature, formatWind, UNIT_LABELS, units } from "./units";

export type { Locale, MessageKey };
export { normalizeLocale };

const STORAGE_KEY = "xue-locale";

interface LocaleDefinition {
  /** The language's own name, in its own script. Never translated: a picker
   * of thirteen languages is only usable if each row reads as itself. */
  endonym: string;
  /** Value for `<html lang>` and the hreflang alternates. */
  htmlLang: string;
  /** The Open Graph `og:locale` of the page in this language. */
  ogLocale: string;
  /** The tag Intl formats dates and numbers with, where it is not
   * `htmlLang`: Intl writes Nepali in Devanagari digits and Arabic in Arabic-Indic ones, while every number
   * the instrument panel and the map print is in Latin ones. */
  intlLocale?: string;
  /** Protomaps `name:*` label language. Every code here is one the
   * `@protomaps/basemaps` `language_script_pairs` table declares, so the
   * basemap's own labels follow the UI in all thirteen. */
  basemapLang: string;
  messages: Record<MessageKey, string>;
}

/** The thirteen languages, in the order the picker lists them: the two
 * Chinese scripts and English first (the project's own), then the rest by
 * script and proximity — the Latin-script ones together, Turkish closing them
 * before Cyrillic, then Nepali, the one Devanagari language, and Arabic, the
 * one right-to-left script, last. */
const DEFINITIONS: Record<Locale, LocaleDefinition> = {
  zh: { endonym: "简体中文", htmlLang: "zh-CN", ogLocale: "zh_CN", basemapLang: "zh-Hans", messages: zh },
  "zh-Hant": { endonym: "繁體中文", htmlLang: "zh-TW", ogLocale: "zh_TW", basemapLang: "zh-Hant", messages: zhHant },
  en: { endonym: "English", htmlLang: "en", ogLocale: "en_US", basemapLang: "en", messages: en },
  ja: { endonym: "日本語", htmlLang: "ja", ogLocale: "ja_JP", basemapLang: "ja", messages: ja },
  ko: { endonym: "한국어", htmlLang: "ko", ogLocale: "ko_KR", basemapLang: "ko", messages: ko },
  de: { endonym: "Deutsch", htmlLang: "de", ogLocale: "de_DE", basemapLang: "de", messages: de },
  fr: { endonym: "Français", htmlLang: "fr", ogLocale: "fr_FR", basemapLang: "fr", messages: fr },
  es: { endonym: "Español", htmlLang: "es", ogLocale: "es_ES", basemapLang: "es", messages: es },
  pt: { endonym: "Português", htmlLang: "pt", ogLocale: "pt_BR", basemapLang: "pt", messages: pt },
  tr: { endonym: "Türkçe", htmlLang: "tr", ogLocale: "tr_TR", basemapLang: "tr", messages: tr },
  ru: { endonym: "Русский", htmlLang: "ru", ogLocale: "ru_RU", basemapLang: "ru", messages: ru },
  ne: { endonym: "नेपाली", htmlLang: "ne", ogLocale: "ne_NP", intlLocale: "ne-u-nu-latn", basemapLang: "ne", messages: ne },
  ar: { endonym: "العربية", htmlLang: "ar", ogLocale: "ar_SA", intlLocale: "ar-u-nu-latn", basemapLang: "ar", messages: ar },
};

/** What the picker lists, in order. */
export const LOCALES: readonly { code: Locale; endonym: string }[] = (
  Object.keys(DEFINITIONS) as Locale[]
).map((code) => ({ code, endonym: DEFINITIONS[code].endonym }));

/** The `<html lang>` tag of any of the thirteen — what the picker stamps on each
 * row so a screen reader reads each endonym in its own language. */
export function localeHtmlLang(code: Locale): string {
  return DEFINITIONS[code].htmlLang;
}

/** The Open Graph locale of any of the thirteen (`og:locale`). */
export function localeOgLocale(code: Locale): string {
  return DEFINITIONS[code].ogLocale;
}

/** The dictionary of any of the thirteen, for the build that writes each
 * page's copies (`web/tooling/sitepages.ts`); the shell reads `t`. */
export function localeMessages(code: Locale): Record<MessageKey, string> {
  return DEFINITIONS[code].messages;
}

/** Every dataset the viewer knows, page or not, for telling a link to a
 * dataset's page from any other root-relative link (a link the build wrote
 * for one it does not serve is a 404 in any language). */
const DATASET_PATH_IDS: ReadonlySet<string> = new Set(FORECAST_MODEL_IDS);

/** Whether the page is one of the site's per-language copies
 * (`web/tooling/sitepages.ts`): under a language's prefix, or the English
 * copy, which carries the language chooser. A build that ships the viewer
 * alone (an embedding, docs/contribution/building-on-xue.md) has no such
 * copies: there the address names no language and no case page — the
 * locale is a link's `?lang=`, else the stored choice, else the browser's,
 * and a case stays `?case=<id>` on the page. */
export const LANGUAGE_PATHS: boolean =
  typeof document !== "undefined" &&
  (splitLocalePath(window.location.pathname).prefixed || document.querySelector("script[data-locale-chooser]") !== null);

/** The locale of a page with no language in its address. */
function chosenLocale(): Locale {
  const fromUrl = normalizeLocale(new URLSearchParams(window.location.search).get("lang"));
  if (fromUrl) return fromUrl;
  try {
    const stored = normalizeLocale(localStorage.getItem(STORAGE_KEY));
    if (stored) return stored;
  } catch {
    // Storage can be unavailable (privacy modes); browser language decides.
  }
  for (const tag of navigator.languages ?? [navigator.language]) {
    const match = normalizeLocale(tag);
    if (match) return match;
  }
  return "en";
}

function detectLocale(): Locale {
  // Unit tests import this module under jsdom/node; never require a browser.
  if (typeof window === "undefined") return "en";
  if (!LANGUAGE_PATHS) return chosenLocale();
  // An old link naming its language as `?lang=` or its case as `/?case=`
  // moves to the path it belongs at before anything reads the address. A
  // storm or airport page has languages of its own and is left alone.
  const { path } = splitLocalePath(window.location.pathname);
  const moved = isShellPath(path, DATASET_PATH_IDS) ? legacyLocaleLocation(window.location.pathname, window.location.search) : null;
  if (moved !== null) window.history.replaceState(null, "", `${moved}${window.location.hash}`);
  return splitLocalePath(window.location.pathname).locale;
}

export let locale: Locale = detectLocale();

let active = DEFINITIONS[locale];

/** Language for the Protomaps basemap labels — the map follows the UI. */
export let basemapLang: string = active.basemapLang;

/** Value for <html lang>. */
export let htmlLang: string = active.htmlLang;

/** The tag every Intl formatter is built on. */
export let intlLocale: string = active.intlLocale ?? active.htmlLang;

const listeners = new Set<() => void>();

/** Run `listener` after every language switch, once the dictionary and the
 * static markup already read in the new language; the listener rewrites
 * whatever copy it composed itself. */
export function onLocaleChange(listener: () => void): void {
  listeners.add(listener);
}

/** The quantities copy names with a unit, in the viewer's units
 * (`units.ts`): the freezing point, the fire-weather gust threshold
 * (10 m/s) and the distance unit. Filled into any message that names them,
 * so a caller never passes them and a units change only has to re-run `t`. */
function unitParams(): Record<string, string> {
  const freezing = convertTemperature(0);
  return {
    freezing: `${freezing} ${UNIT_LABELS.temperature[units.temperature]}`,
    fwGust: formatWind(10, 0),
    distanceUnit: UNIT_LABELS.distance[units.distance],
  };
}

const ARABIC_SCRIPT = /[؀-ۿ]/;

/** The message `key` with `params` put in.
 *
 * Arabic is the one right-to-left language, and the shell is not mirrored
 * for it, so its text is isolated rather than laid out by the page: an Arabic
 * message goes in a right-to-left isolate (U+2067 … U+2069) and each value
 * in it in a first-strong one (U+2068 … U+2069). The message then reads
 * right to left in any left-to-right box, and the shell may put a value
 * after it by hand (`Model 51 m`) without the bidi algorithm pulling the
 * number into the Arabic run; a value inside it (`44.00°N 58.75°E`, a time,
 * a number with its unit) keeps its digits and letters together. */
export function t(key: MessageKey, params?: Record<string, string | number>): string {
  let text: string = active.messages[key];
  const isolate = locale === "ar";
  if (text.includes("{")) {
    for (const [name, value] of Object.entries({ ...unitParams(), ...params })) {
      text = text.replace(`{${name}}`, isolate ? `⁨${value}⁩` : String(value));
    }
  }
  return isolate && ARABIC_SCRIPT.test(text) ? `⁧${text}⁩` : text;
}

/** Rewrites every element carrying data-i18n / data-i18n-aria /
 * data-i18n-content / data-i18n-tip from the dictionary, stamps <html
 * lang> and the meta description, and points the links to the shell's
 * pages at this language's copies. The build writes each language's copy
 * of a page already translated (`web/tooling/sitepages.ts`); the source
 * markup carries the English. */
export function applyStaticMessages(): void {
  document.documentElement.lang = htmlLang;
  // The one parameter static copy takes: the year of distribution, which
  // EUMETSAT's attribution sentence carries (`creditsMeteosat`). Read at
  // each call so a tab left open across New Year restates it on the next
  // locale switch.
  const params = { year: new Date().getUTCFullYear() };
  for (const element of document.querySelectorAll<HTMLElement>("[data-i18n]")) {
    element.textContent = t(element.dataset.i18n as MessageKey, params);
  }
  for (const element of document.querySelectorAll<HTMLElement>("[data-i18n-aria]")) {
    element.setAttribute("aria-label", t(element.dataset.i18nAria as MessageKey));
  }
  for (const element of document.querySelectorAll<HTMLElement>("[data-i18n-content]")) {
    element.setAttribute("content", t(element.dataset.i18nContent as MessageKey));
  }
  // The stylesheet's hover tooltip reads `data-tip` (attr()), so the copy
  // lands there rather than in a title, which would show a second, native one.
  for (const element of document.querySelectorAll<HTMLElement>("[data-i18n-tip]")) {
    element.dataset.tip = t(element.dataset.i18nTip as MessageKey);
  }
  localizeLinks();
}

/** A root-relative link to one of the site's pages, in the page's
 * language, or the link as it was: `/showcase` on a Japanese page is
 * `/ja/showcase`, a storm or airport page the one in that language or,
 * where there is none, in English (`tc/pages.ts::pagePrefix`). A build
 * with no language copies (`LANGUAGE_PATHS`) keeps every link. */
export function localizedHref(href: string): string {
  if (!LANGUAGE_PATHS || !href.startsWith("/") || href.startsWith("//")) return href;
  const url = new URL(href, "https://localhost");
  const { path } = splitLocalePath(url.pathname);
  if (isStaticPagePath(path)) return `${pagePrefix(locale)}${path}${url.search}${url.hash}`;
  if (!isShellPath(path, DATASET_PATH_IDS)) return href;
  return `${localizedPath(locale, path)}${url.search}${url.hash}`;
}

/** The address of a page of the shell in the page's language
 * (`sitepaths.ts::localizedPath`). */
export function shellPath(path: string): string {
  return LANGUAGE_PATHS ? localizedPath(locale, path) : path;
}

/** A link to a case: its page (`/ja/showcase/<id>`), or on a build with
 * no case pages (`LANGUAGE_PATHS`) the viewer naming it in the query. */
export function caseHref(id: string): string {
  return LANGUAGE_PATHS ? localizedPath(locale, casePathname(id)) : `./?case=${encodeURIComponent(id)}`;
}

/** Point every link to one of the site's pages at the page's language. */
function localizeLinks(): void {
  if (!LANGUAGE_PATHS) return;
  for (const link of document.querySelectorAll<HTMLAnchorElement>("a[href^='/']")) {
    const href = link.getAttribute("href")!;
    const next = localizedHref(href);
    if (next !== href) link.setAttribute("href", next);
  }
}

/** Switch languages in place: remember the choice on this device, move the
 * address to the new language's prefix (`replaceState`, no reload), swap
 * the dictionary, rewrite the static markup and let every listener redo
 * its own copy. A link copied afterwards names the new language, as the
 * page now is. */
export function setLocale(next: Locale): void {
  if (next === locale) return;
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // Without storage the choice lasts this page load only.
  }
  if (LANGUAGE_PATHS) {
    const pathname = relocalizePath(window.location.pathname, next);
    if (pathname !== window.location.pathname) {
      window.history.replaceState(null, "", `${pathname}${window.location.search}${window.location.hash}`);
    }
  } else {
    // The choice lives on this device: a link copied afterwards opens in
    // each reader's own language, so an explicit `?lang=` goes too.
    const params = new URLSearchParams(window.location.search);
    params.delete("lang");
    const search = params.size > 0 ? `?${params.toString()}` : "";
    if (search !== window.location.search) {
      window.history.replaceState(null, "", `${window.location.pathname}${search}${window.location.hash}`);
    }
  }
  locale = next;
  active = DEFINITIONS[next];
  basemapLang = active.basemapLang;
  htmlLang = active.htmlLang;
  intlLocale = active.intlLocale ?? active.htmlLang;
  applyStaticMessages();
  for (const listener of listeners) listener();
}
